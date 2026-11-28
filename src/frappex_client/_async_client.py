"""The asyncio client."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import ssl
from collections.abc import AsyncIterator, Awaitable, Iterable, Mapping, Sequence
from types import TracebackType
from typing import Any, BinaryIO, Literal, TypeVar

import httpx

from . import _operations as ops
from ._auth import Auth, PasswordAuth
from ._base import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT,
    ClientCore,
    ResponseSize,
    logger,
)
from ._config import RetryPolicy
from ._protocol import RawResponse, Request, decode, file_path, parse_retry_after
from .exceptions import (
    AuthenticationError,
    BulkOperationError,
    FrappexError,
    PermissionDeniedError,
    ReadOnlyError,
    TransportError,
    UnexpectedResponseError,
)
from .types import BulkUpdateFailure, Document, Filters, ReportResult

T = TypeVar("T")
R = TypeVar("R")


class AsyncClient(ClientCore):
    """Asyncio client for one Frappe site.

    It has the same methods as :class:`~frappex_client.Client`, awaited. Use it as an
    async context manager so connections (and a password session) are closed
    when you are done::

        async with AsyncClient("https://erp.example.com", TokenAuth(key, secret)) as client:
            async for todo in client.iter_list("ToDo", filters={"status": "Open"}):
                print(todo["name"])

    Args:
        base_url: Root address of the site. Must be ``https://`` unless
            ``allow_insecure_http`` is set.
        auth: A :class:`~frappex_client.TokenAuth`, :class:`~frappex_client.BearerAuth` or
            :class:`~frappex_client.PasswordAuth`. Omit it for guest access: no
            credentials are sent, so only what the site opens to guests
            works, such as :meth:`ping`, public files and methods
            whitelisted with ``allow_guest``.
        timeout: Seconds allowed per request, or an ``httpx.Timeout``.
        retry: Retry rules; defaults to :class:`~frappex_client.RetryPolicy`.
        verify: Path of a CA bundle or an ``ssl.SSLContext`` for sites with a
            private certificate. Certificate checks cannot be disabled.
        allow_insecure_http: Permit ``http://`` for a local development site.
        max_response_bytes: Largest response body accepted, after
            decompression.
        headers: Extra headers for every request, such as ``Host`` for a
            multi-site bench. ``Authorization`` and ``Cookie`` are refused.
        transport: An async ``httpx`` transport to use instead of the default, for
            tests or custom networking. It then owns TLS, so ``verify`` no
            longer applies.
        max_concurrency: Most requests in flight at once (10 by default).
            Further requests wait for a free slot instead of failing, and the
            connection pool is sized to match. ``None`` removes the limit.
        max_request_time: Longest one request attempt may take, from sending
            to the last byte of the reply, headers included, in seconds. It
            stops a site that sends a reply very slowly from holding the
            client forever. By default 300, or twice the read ``timeout`` if
            that is longer; ``math.inf`` removes it.
        http2: Offer HTTP/2, which lets many concurrent requests share one
            connection. Needs ``pip install "frappex-client[http2]"``. Sites
            that do not support it are spoken to over HTTP/1.1, and so is a
            plain ``http://`` site.
        read_only: Send only GET and HEAD requests for data. Inserts,
            updates, deletes, submit, cancel, rename, uploads and methods
            called by POST raise :class:`~frappex_client.ReadOnlyError`
            before anything is sent; reads, reports and downloads work.
            Password login and logout are still sent. A whitelisted method
            called with ``http_method="GET"`` runs as written, so one that
            writes and accepts GET still writes. This guards your own code;
            the API user's roles on the site remain the real limit.

    Raises:
        ConfigurationError: A setting is malformed or unsafe.
    """

    def __init__(
        self,
        base_url: str,
        auth: Auth | None = None,
        *,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
        retry: RetryPolicy | None = None,
        verify: ssl.SSLContext | str | os.PathLike[str] | None = None,
        allow_insecure_http: bool = False,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        headers: Mapping[str, str] | None = None,
        read_only: bool = False,
        max_concurrency: int | None = DEFAULT_MAX_CONCURRENCY,
        http2: bool = False,
        max_request_time: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Validate the settings and prepare the connection pool."""
        super().__init__(
            base_url,
            auth,
            timeout=timeout,
            retry=retry,
            verify=verify,
            allow_insecure_http=allow_insecure_http,
            max_response_bytes=max_response_bytes,
            headers=headers,
            read_only=read_only,
            max_concurrency=max_concurrency,
            http2=http2,
            max_request_time=max_request_time,
        )
        self._http = httpx.AsyncClient(transport=transport, **self._http_arguments)
        # Held only while a request is on the wire, never during a retry wait.
        self._slots: asyncio.Semaphore | contextlib.nullcontext[None] = (
            asyncio.Semaphore(max_concurrency)
            if max_concurrency is not None
            else contextlib.nullcontext()
        )
        self._login_lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        """End a password session, if any, and release connections."""
        try:
            if self._logged_in:
                self._logged_in = False
                try:
                    await self._run(ops.logout())
                except FrappexError:
                    logger.debug("logout failed; the session will expire on its own")
        finally:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncClient:
        """Return the client itself."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the client."""
        await self.aclose()

    # -- transport ---------------------------------------------------------

    async def _send(self, request: Request) -> RawResponse:
        async with self._slots:
            return await self._within_deadline(request, self._send_now(request))

    async def _within_deadline(self, request: Request, work: Awaitable[R]) -> R:
        """Run ``work``, stopping it when the request takes too long overall.

        This covers a reply whose headers or body arrive a byte at a time,
        which per-read timeouts alone never stop.
        """
        try:
            return await asyncio.wait_for(work, self._max_request_time)
        except asyncio.TimeoutError:
            raise self._too_slow(request) from None

    async def _send_now(self, request: Request) -> RawResponse:
        try:
            async with self._http.stream(
                request.method, request.path, **self._request_arguments(request)
            ) as response:
                self._check_content_encoding(response.headers, response.status_code)
                self._check_declared_length(response.headers, self._max_response_bytes)
                size = ResponseSize(self._max_response_bytes)
                chunks: list[bytes] = []
                async for chunk in response.aiter_bytes():
                    size.add(len(chunk))
                    chunks.append(chunk)
                return RawResponse(
                    response.status_code,
                    response.headers,
                    b"".join(chunks),
                    response.http_version,
                )
        except httpx.HTTPError as exc:
            raise self._transport_error(request, exc) from exc

    async def _run(self, operation: ops.Operation[T]) -> T:
        request = operation.request
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._send(request)
            except TransportError:
                delay = self._retry.delay_after_failure(request.method, attempt)
                if delay is None:
                    raise
            else:
                logger.debug(
                    "%s %s -> %s (%s)",
                    request.method,
                    request.path,
                    response.status_code,
                    response.http_version,
                )
                delay = self._retry.delay_after_response(
                    request.method,
                    attempt,
                    response.status_code,
                    parse_retry_after(response.headers),
                )
                if delay is None:
                    return operation.parse(decode(response))
            await asyncio.sleep(delay)

    async def _ensure_session(self) -> int:
        """Log in once when the client uses password authentication.

        Returns:
            The number of the session in use, to tell later whether another
            caller has renewed it meanwhile.
        """
        if self._needs_login:
            async with self._login_lock:
                if self._needs_login and isinstance(self._auth, PasswordAuth):
                    try:
                        await self._run(
                            ops.login(self._auth.username, self._auth.password.reveal())
                        )
                    except FrappexError as error:
                        # Drop the chained httpx exception: its traceback frames
                        # hold the login body, password included, where error
                        # trackers that record local variables would find it.
                        error.__cause__ = error.__context__ = None
                        error.__suppress_context__ = True
                        raise
                    self._logged_in = True
                    self._session += 1
        return self._session

    async def _session_expired(self) -> bool:
        """Whether a password session has ended on the site.

        Frappe treats an expired session as a guest, so a request then fails
        with 401 or 403 exactly like a missing permission. Asking who is
        logged in tells the two apart. Other kinds of authentication never
        expire here and cost no request.
        """
        if not (isinstance(self._auth, PasswordAuth) and self._logged_in):
            return False
        try:
            user = await self._run(ops.get_logged_user())
        except (AuthenticationError, PermissionDeniedError):
            return True
        return user == "Guest"

    async def _renew_session(self, seen: int) -> None:
        """Log in again, unless another caller already renewed session ``seen``."""
        async with self._login_lock:
            if self._session == seen:
                self._logged_in = False
        await self._ensure_session()

    async def _execute(self, operation: ops.Operation[T]) -> T:
        self._refuse_writes(operation.request)
        session = await self._ensure_session()
        try:
            return await self._run(operation)
        except (AuthenticationError, PermissionDeniedError):
            # A request refused as unauthenticated never ran, so repeating it
            # once on a fresh session is safe even for writes.
            # Another caller may be renewing the session right now (it has
            # cleared _logged_in); then wait for that login instead of failing.
            if not isinstance(self._auth, PasswordAuth) or (
                self._session == session and self._logged_in and not await self._session_expired()
            ):
                raise
            await self._renew_session(session)
            return await self._run(operation)

    # -- site --------------------------------------------------------------

    async def ping(self) -> str:
        """Check that the site answers. Returns ``"pong"``."""
        return await self._run(ops.ping())

    async def get_logged_user(self) -> str:
        """Return the user the credentials belong to.

        A cheap way to confirm that the credentials work.
        """
        return await self._execute(ops.get_logged_user())

    # -- documents ---------------------------------------------------------

    async def get_list(
        self,
        doctype: str,
        *,
        fields: Sequence[str] | None = None,
        filters: Filters | None = None,
        or_filters: Filters | None = None,
        order_by: str | None = None,
        limit: int = 20,
        offset: int = 0,
        parent: str | None = None,
    ) -> list[Document]:
        """Return one page of documents.

        Args:
            doctype: DocType to list.
            fields: Fields to return; the site returns only ``name`` if omitted.
            filters: Conditions every row must meet.
            or_filters: Conditions of which a row must meet at least one.
            order_by: Sort clause, such as ``"modified desc"``.
            limit: Maximum rows to return.
            offset: Rows to skip.
            parent: Parent DocType, required when listing a child table.

        See :meth:`iter_list` to read every matching row.
        """
        return await self._execute(
            ops.get_list(
                doctype,
                fields=fields,
                filters=filters,
                or_filters=or_filters,
                order_by=order_by,
                limit=limit,
                offset=offset,
                parent=parent,
            )
        )

    async def iter_list(
        self,
        doctype: str,
        *,
        fields: Sequence[str] | None = None,
        filters: Filters | None = None,
        or_filters: Filters | None = None,
        order_by: str | None = None,
        page_size: int = ops.DEFAULT_PAGE_SIZE,
        parent: str | None = None,
    ) -> AsyncIterator[Document]:
        """Yield every matching document, fetching ``page_size`` rows at a time.

        Rows come in creation order unless ``order_by`` is given. With that
        default order and plain field names, pages are read by keyset (rows
        created at or after the last one seen), so each page costs the same
        however deep the iteration goes, and rows edited, created or deleted
        meanwhile cannot shift a page. A custom ``order_by``, or computed or
        child-table fields, uses offset paging instead.
        """
        pager = ops.ListPager(
            doctype,
            fields=fields,
            filters=filters,
            or_filters=or_filters,
            order_by=order_by,
            page_size=page_size,
            parent=parent,
        )
        while not pager.done:
            rows = await self._execute(pager.next_operation())
            for document in pager.accept(rows):
                yield document

    async def get_doc(self, doctype: str, name: str) -> Document:
        """Return one document.

        Raises:
            NotFoundError: No such document.
        """
        return await self._execute(ops.get_doc(doctype, name))

    async def insert(self, doctype: str, values: Mapping[str, Any]) -> Document:
        """Create a document and return it as saved.

        This request is never retried automatically, because a repeat could
        create a duplicate.
        """
        return await self._execute(ops.insert(doctype, values))

    async def update(self, doctype: str, name: str, values: Mapping[str, Any]) -> Document:
        """Change the given fields of a document and return it as saved."""
        return await self._execute(ops.update(doctype, name, values))

    async def delete(self, doctype: str, name: str) -> None:
        """Delete a document."""
        await self._execute(ops.delete(doctype, name))

    async def submit(self, doctype: str, name: str) -> None:
        """Submit a draft document."""
        await self._execute(ops.submit(doctype, name))

    async def cancel(self, doctype: str, name: str) -> None:
        """Cancel a submitted document."""
        await self._execute(ops.cancel(doctype, name))

    async def rename(
        self, doctype: str, old_name: str, new_name: str, *, merge: bool = False
    ) -> str:
        """Rename a document and return its new name.

        Args:
            doctype: DocType of the document.
            old_name: Current name.
            new_name: Name to give it.
            merge: Merge into an existing document called ``new_name``.
        """
        return await self._execute(ops.rename(doctype, old_name, new_name, merge=merge))

    async def get_count(self, doctype: str, filters: Filters | None = None) -> int:
        """Return how many documents match ``filters``."""
        return await self._execute(ops.get_count(doctype, filters))

    async def get_value(
        self,
        doctype: str,
        fieldname: str | Sequence[str],
        filters: Filters | str | None = None,
    ) -> Document | None:
        """Return the requested fields of the first matching document.

        Args:
            doctype: DocType to read.
            fieldname: One field name or a list of them.
            filters: Conditions, or a document name.

        Returns:
            A mapping of field name to value, or ``None`` if nothing matches.
        """
        return await self._execute(ops.get_value(doctype, fieldname, filters))

    async def set_value(self, doctype: str, name: str, values: Mapping[str, Any]) -> Document:
        """Set fields on a document and return it as saved."""
        return await self._execute(ops.set_value(doctype, name, values))

    async def insert_many(
        self,
        doctype: str,
        docs: Iterable[Mapping[str, Any]],
        *,
        batch_size: int = ops.MAX_BATCH_SIZE,
    ) -> list[str]:
        """Insert many documents of one DocType and return their names.

        Documents are sent in batches of ``batch_size``. Frappe saves each
        batch completely or not at all; earlier batches stay saved if a later
        one fails. Like every write, a batch is not retried after a network or
        server error, only after HTTP 429.

        Args:
            doctype: DocType of every document.
            docs: Field values for each new document.
            batch_size: Documents per request, from 1 to 200 (Frappe's limit).

        Returns:
            The new documents' names, in the order given.

        Raises:
            BulkOperationError: A batch failed. It records the names already
                saved, where to resume, and the original error.
        """
        prepared = ops.prepare_inserts(doctype, docs)
        names: list[str] = []
        for start, batch in ops.batches(prepared, batch_size):
            try:
                saved = await self._execute(ops.insert_many(batch))
            except ReadOnlyError:
                raise
            except FrappexError as exc:
                raise BulkOperationError(
                    completed=names, failures=[], failed_at=start, error=exc
                ) from exc
            names.extend(saved)
        return names

    async def bulk_update(
        self,
        doctype: str,
        updates: Mapping[str, Mapping[str, Any]],
        *,
        batch_size: int = ops.MAX_BATCH_SIZE,
    ) -> list[BulkUpdateFailure]:
        """Change fields on many documents of one DocType.

        Frappe saves each document separately, so one invalid document does
        not stop the others. A document that raised is reported, but Frappe
        does not roll it back: changes written before the error, for example
        before an ``on_update`` hook failed, can still be committed.

        Updates are sent in batches of ``batch_size``; like every write, a
        batch is not retried after a network or server error.

        Args:
            doctype: DocType of every document.
            updates: Maps each document name to the fields to set, such as
                ``{"TODO-0001": {"status": "Closed"}}``.
            batch_size: Documents per request, from 1 to 200.

        Returns:
            The documents for which the site reported an error; empty when
            every document was saved.

        Raises:
            BulkOperationError: A whole batch failed, for example because of a
                network error or missing permission.
        """
        prepared = ops.prepare_updates(doctype, updates)
        completed: list[str] = []
        failures: list[BulkUpdateFailure] = []
        for start, batch in ops.batches(prepared, batch_size):
            try:
                failed = await self._execute(ops.bulk_update(doctype, batch))
            except ReadOnlyError:
                raise
            except FrappexError as exc:
                raise BulkOperationError(
                    completed=completed, failures=failures, failed_at=start, error=exc
                ) from exc
            failed_names = {failure.name for failure in failed}
            completed += [name for name, _ in batch if name not in failed_names]
            failures += failed
        return failures

    # -- methods -----------------------------------------------------------

    async def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        http_method: Literal["GET", "POST"] = "POST",
    ) -> Any:
        """Call a whitelisted server method and return its result.

        Args:
            method: Dotted path, such as ``"myapp.api.get_summary"``.
            params: Arguments for the method.
            http_method: ``"POST"`` (default) sends the arguments as JSON and
                is never retried. Use ``"GET"`` for a read-only method that is
                safe to repeat.
        """
        return await self._execute(ops.call(method, params, http_method))

    async def run_report(
        self, report_name: str, filters: Mapping[str, Any] | None = None
    ) -> ReportResult:
        """Run a Query, Script or Custom Report and return its columns and rows.

        Args:
            report_name: Name of the report, such as ``"General Ledger"``.
            filters: The report's filters, such as ``{"company": "Acme"}``.

        Returns:
            The columns, the rows keyed by column field name, and the totals
            row when the report adds one. For a report that runs in the
            background, the rows of its last completed run with these filters,
            with ``prepared`` set.

        Raises:
            PreparedReportError: The report runs in the background and has no
                completed run for these filters, or its last run failed.
        """
        return await self._execute(ops.run_report(report_name, filters))

    # -- files -------------------------------------------------------------

    async def upload_file(
        self,
        filename: str,
        content: bytes,
        *,
        is_private: bool = True,
        doctype: str | None = None,
        docname: str | None = None,
        fieldname: str | None = None,
        folder: str | None = None,
    ) -> Document:
        """Upload a file and return its ``File`` document.

        Files are private unless ``is_private=False`` is passed: a public
        file can be fetched by anyone who has its URL.

        Args:
            filename: Name to store the file under.
            content: File contents.
            is_private: Keep the file behind the site's permissions.
            doctype: DocType of the document to attach the file to.
            docname: Name of the document to attach the file to.
            fieldname: Attach field on that document to fill in.
            folder: Folder in the file manager; the site defaults to ``Home``.
        """
        return await self._execute(
            ops.upload_file(
                filename,
                content,
                is_private=is_private,
                doctype=doctype,
                docname=docname,
                fieldname=fieldname,
                folder=folder,
            )
        )

    async def download_to(
        self, file_url: str, target: BinaryIO, *, max_bytes: int | None = None
    ) -> int:
        """Stream a file from the site into ``target``.

        Args:
            file_url: Site-relative location, such as ``/private/files/a.pdf``
                (the ``file_url`` field of a ``File`` document).
            target: Writable binary file object.
            max_bytes: Stop with :class:`~frappex_client.ResponseTooLargeError` after
                this many bytes; unlimited when omitted.

        Returns:
            Number of bytes written.

        A download is not retried, since part of it may already be written.
        """
        return await self._stream_to(Request("GET", file_path(file_url)), target, max_bytes)

    async def _stream_to(
        self,
        request: Request,
        target: BinaryIO,
        max_bytes: int | None,
        content_type: str | None = None,
    ) -> int:
        """Send ``request`` and write a successful body into ``target``."""
        self._refuse_writes(request)
        session = await self._ensure_session()
        try:
            return await self._stream_once(request, target, max_bytes, content_type)
        except (AuthenticationError, PermissionDeniedError):
            # Another caller may be renewing the session right now (it has
            # cleared _logged_in); then wait for that login instead of failing.
            if not isinstance(self._auth, PasswordAuth) or (
                self._session == session and self._logged_in and not await self._session_expired()
            ):
                raise
            await self._renew_session(session)
            return await self._stream_once(request, target, max_bytes, content_type)

    async def _stream_once(
        self,
        request: Request,
        target: BinaryIO,
        max_bytes: int | None,
        content_type: str | None,
    ) -> int:
        """One attempt at a download; nothing is written unless it succeeds."""

        async def stream() -> int:
            try:
                async with self._http.stream(
                    request.method, request.path, **self._request_arguments(request)
                ) as response:
                    self._check_content_encoding(response.headers, response.status_code)
                    if response.status_code != 200:
                        size = ResponseSize(self._max_response_bytes)
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            size.add(len(chunk))
                            body += chunk
                        decode(RawResponse(response.status_code, response.headers, bytes(body)))
                        raise UnexpectedResponseError(
                            f"HTTP {response.status_code}: expected 200 for a download.",
                            status_code=response.status_code,
                        )
                    self._check_content_type(response.headers, content_type)
                    self._check_declared_length(response.headers, max_bytes)
                    size = ResponseSize(max_bytes)
                    async for chunk in response.aiter_bytes():
                        size.add(len(chunk))
                        target.write(chunk)
                    return size.total
            except httpx.HTTPError as exc:
                raise self._transport_error(request, exc) from exc

        async with self._slots:
            return await self._within_deadline(request, stream())

    async def download(self, file_url: str) -> bytes:
        """Return a file's contents, up to ``max_response_bytes``.

        Use :meth:`download_to` for large files.
        """
        buffer = io.BytesIO()
        await self.download_to(file_url, buffer, max_bytes=self._max_response_bytes)
        return buffer.getvalue()

    async def download_pdf_to(
        self,
        doctype: str,
        name: str,
        target: BinaryIO,
        *,
        print_format: str | None = None,
        letterhead: str | None = None,
        no_letterhead: bool = False,
        language: str | None = None,
        max_bytes: int | None = None,
    ) -> int:
        """Render a document's print format as a PDF and stream it into ``target``.

        Args:
            doctype: DocType of the document, such as ``"Sales Invoice"``.
            name: Name of the document.
            print_format: Print format to use; the DocType's default when omitted.
            letterhead: Letter head to print with; the default when omitted.
            no_letterhead: Print without any letter head.
            language: Language code to print in, such as ``"de"``.
            target: Writable binary file object.
            max_bytes: Stop with :class:`~frappex_client.ResponseTooLargeError` after
                this many bytes; unlimited when omitted.

        Returns:
            Number of bytes written.

        Raises:
            UnexpectedResponseError: The site answered with something other
                than a PDF, such as an HTML page.

        Like every download, it is not retried.
        """
        request = ops.pdf_request(
            doctype,
            name,
            print_format=print_format,
            letterhead=letterhead,
            no_letterhead=no_letterhead,
            language=language,
        )
        return await self._stream_to(request, target, max_bytes, ops.PDF_CONTENT_TYPE)

    async def download_pdf(
        self,
        doctype: str,
        name: str,
        *,
        print_format: str | None = None,
        letterhead: str | None = None,
        no_letterhead: bool = False,
        language: str | None = None,
    ) -> bytes:
        """Return a document's print format as PDF bytes, up to ``max_response_bytes``.

        Args:
            doctype: DocType of the document, such as ``"Sales Invoice"``.
            name: Name of the document.
            print_format: Print format to use; the DocType's default when omitted.
            letterhead: Letter head to print with; the default when omitted.
            no_letterhead: Print without any letter head.
            language: Language code to print in, such as ``"de"``.

        Use :meth:`download_pdf_to` to write a large PDF straight to a file.
        """
        buffer = io.BytesIO()
        await self.download_pdf_to(
            doctype,
            name,
            buffer,
            print_format=print_format,
            letterhead=letterhead,
            no_letterhead=no_letterhead,
            language=language,
            max_bytes=self._max_response_bytes,
        )
        return buffer.getvalue()
