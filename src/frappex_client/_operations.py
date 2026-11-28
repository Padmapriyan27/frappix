"""Every API call frappex-client can make, described as data.

Each function returns an :class:`Operation`: the request to send and a
function that picks the result out of the decoded reply. Nothing here
touches the network, which is what lets the sync and async clients share one
implementation of the Frappe API.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

from ._protocol import Request, method_path, resource_path
from .exceptions import ConfigurationError, PreparedReportError, UnexpectedResponseError
from .types import BulkUpdateFailure, Document, Filters, ReportResult

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 100
STABLE_ORDER = "creation asc, name asc"

# frappe.client.insert_many refuses more than 200 documents per request.
MAX_BATCH_SIZE = 200
# Keys bulk_update sets itself; letting values override them would retarget
# or rename the document.
_UPDATE_RESERVED_KEYS = frozenset({"doctype", "docname", "name"})


@dataclass(frozen=True)
class Operation(Generic[T]):
    """A request paired with the parser for its reply."""

    request: Request
    parse: Callable[[dict[str, Any]], T]


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _data(payload: dict[str, Any]) -> Any:
    return payload.get("data")


def _message(payload: dict[str, Any]) -> Any:
    return payload.get("message")


def _nothing(payload: dict[str, Any]) -> None:
    return None


def _document(source: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Document]:
    def parse(payload: dict[str, Any]) -> Document:
        value = source(payload)
        if not isinstance(value, dict):
            raise UnexpectedResponseError("The reply does not contain a document.", status_code=200)
        return value

    return parse


def _documents(payload: dict[str, Any]) -> list[Document]:
    value = _data(payload)
    if not isinstance(value, list):
        raise UnexpectedResponseError("The reply does not contain a list.", status_code=200)
    return value


def ping() -> Operation[str]:
    """Check that the site answers; needs no permissions."""
    return Operation(Request("GET", method_path("ping")), lambda p: str(_message(p)))


def get_logged_user() -> Operation[str]:
    """Return the user the credentials belong to."""
    return Operation(
        Request("GET", method_path("frappe.auth.get_logged_user")), lambda p: str(_message(p))
    )


def login(username: str, password: str) -> Operation[None]:
    """Open a cookie session."""
    return Operation(
        Request("POST", method_path("login"), json={"usr": username, "pwd": password}), _nothing
    )


def logout() -> Operation[None]:
    """Close the cookie session."""
    return Operation(Request("POST", method_path("logout")), _nothing)


def get_list(
    doctype: str,
    *,
    fields: Sequence[str] | None = None,
    filters: Filters | None = None,
    or_filters: Filters | None = None,
    order_by: str | None = None,
    limit: int = 20,
    offset: int = 0,
    parent: str | None = None,
) -> Operation[list[Document]]:
    """List documents of ``doctype``."""
    if limit < 1:
        raise ConfigurationError("limit must be at least 1; use iter_list to read every row.")
    if offset < 0:
        raise ConfigurationError("offset must not be negative.")
    params = {"limit_page_length": str(limit), "limit_start": str(offset)}
    if fields is not None:
        if isinstance(fields, str):
            raise ConfigurationError("fields must be a list of field names, not a string.")
        params["fields"] = _dumps(list(fields))
    if filters is not None:
        params["filters"] = _dumps(filters)
    if or_filters is not None:
        params["or_filters"] = _dumps(or_filters)
    if order_by is not None:
        params["order_by"] = order_by
    if parent is not None:
        params["parent"] = parent
    return Operation(Request("GET", resource_path(doctype), params=params), _documents)


def get_doc(doctype: str, name: str) -> Operation[Document]:
    """Fetch one document."""
    return Operation(Request("GET", resource_path(doctype, name)), _document(_data))


def insert(doctype: str, values: Mapping[str, Any]) -> Operation[Document]:
    """Create a document."""
    return Operation(Request("POST", resource_path(doctype), json=dict(values)), _document(_data))


def update(doctype: str, name: str, values: Mapping[str, Any]) -> Operation[Document]:
    """Change fields of a document."""
    return Operation(
        Request("PUT", resource_path(doctype, name), json=dict(values)), _document(_data)
    )


def delete(doctype: str, name: str) -> Operation[None]:
    """Delete a document."""
    return Operation(Request("DELETE", resource_path(doctype, name)), _nothing)


def submit(doctype: str, name: str) -> Operation[None]:
    """Submit a document through its whitelisted ``submit`` method."""
    return Operation(
        Request("POST", resource_path(doctype, name), json={"run_method": "submit"}), _nothing
    )


def cancel(doctype: str, name: str) -> Operation[None]:
    """Cancel a submitted document."""
    return Operation(
        Request(
            "POST", method_path("frappe.client.cancel"), json={"doctype": doctype, "name": name}
        ),
        _nothing,
    )


def rename(doctype: str, old_name: str, new_name: str, *, merge: bool) -> Operation[str]:
    """Rename a document and return its new name."""
    body = {"doctype": doctype, "old_name": old_name, "new_name": new_name, "merge": merge}
    return Operation(
        Request("POST", method_path("frappe.client.rename_doc"), json=body),
        lambda p: str(_message(p)),
    )


def get_count(doctype: str, filters: Filters | None) -> Operation[int]:
    """Count documents matching ``filters``."""
    params = {"doctype": doctype}
    if filters is not None:
        params["filters"] = _dumps(filters)

    def parse(payload: dict[str, Any]) -> int:
        value = _message(payload)
        if isinstance(value, bool) or not isinstance(value, int):
            raise UnexpectedResponseError("The reply does not contain a count.", status_code=200)
        return value

    return Operation(Request("GET", method_path("frappe.client.get_count"), params=params), parse)


def get_value(
    doctype: str, fieldname: str | Sequence[str], filters: Filters | str | None
) -> Operation[Document | None]:
    """Read one or more fields of the first matching document."""
    params = {
        "doctype": doctype,
        "fieldname": fieldname if isinstance(fieldname, str) else _dumps(list(fieldname)),
    }
    if filters is not None:
        params["filters"] = filters if isinstance(filters, str) else _dumps(filters)

    def parse(payload: dict[str, Any]) -> Document | None:
        value = _message(payload)
        # Frappe answers with an empty object when no document matches.
        return value if isinstance(value, dict) and value else None

    return Operation(Request("GET", method_path("frappe.client.get_value"), params=params), parse)


def set_value(doctype: str, name: str, values: Mapping[str, Any]) -> Operation[Document]:
    """Set fields on a document and return the saved document."""
    body = {"doctype": doctype, "name": name, "fieldname": dict(values)}
    return Operation(
        Request("POST", method_path("frappe.client.set_value"), json=body), _document(_message)
    )


def call(
    method: str, params: Mapping[str, Any] | None, http_method: Literal["GET", "POST"]
) -> Operation[Any]:
    """Call a whitelisted server method."""
    if http_method not in ("GET", "POST"):
        raise ConfigurationError("http_method must be 'GET' or 'POST'.")
    arguments = dict(params or {})
    if http_method == "POST":
        return Operation(Request("POST", method_path(method), json=arguments), _message)
    query = {
        key: value if isinstance(value, str) else _dumps(value)
        for key, value in arguments.items()
        if value is not None
    }
    return Operation(Request("GET", method_path(method), params=query), _message)


def upload_file(
    filename: str,
    content: bytes,
    *,
    is_private: bool,
    doctype: str | None,
    docname: str | None,
    fieldname: str | None,
    folder: str | None,
) -> Operation[Document]:
    """Upload a file, optionally attached to a document."""
    if not isinstance(filename, str) or not filename or "/" in filename or "\\" in filename:
        raise ConfigurationError("filename must be a bare file name without directories.")
    if not isinstance(content, bytes | bytearray):
        raise ConfigurationError("content must be bytes.")
    form = {"is_private": "1" if is_private else "0"}
    optional = {"doctype": doctype, "docname": docname, "fieldname": fieldname, "folder": folder}
    form.update({key: value for key, value in optional.items() if value is not None})
    return Operation(
        Request("POST", method_path("upload_file"), form=form, file=(filename, bytes(content))),
        _document(_message),
    )


def batches(items: Sequence[T], batch_size: int) -> list[tuple[int, Sequence[T]]]:
    """Split ``items`` into ``(start index, batch)`` pairs.

    Raises:
        ConfigurationError: ``batch_size`` is outside 1 to 200.
    """
    if isinstance(batch_size, bool) or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ConfigurationError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}.")
    return [
        (start, items[start : start + batch_size]) for start in range(0, len(items), batch_size)
    ]


def _check_doctype(doctype: str) -> None:
    if not isinstance(doctype, str) or not doctype.strip():
        raise ConfigurationError("doctype must be a non-empty string.")


def prepare_inserts(doctype: str, docs: Iterable[Mapping[str, Any]]) -> list[Document]:
    """Copy ``docs`` and stamp each one with ``doctype``.

    Raises:
        ConfigurationError: A document is not a mapping, or names another DocType.
    """
    _check_doctype(doctype)
    if isinstance(docs, Mapping | str | bytes):
        raise ConfigurationError("docs must be a list of documents, not a single document.")
    prepared = []
    for index, doc in enumerate(docs):
        if not isinstance(doc, Mapping):
            raise ConfigurationError(f"docs[{index}] must be a mapping of field names to values.")
        values = dict(doc)
        if values.setdefault("doctype", doctype) != doctype:
            raise ConfigurationError(f"docs[{index}] belongs to another DocType.")
        prepared.append(values)
    return prepared


def insert_many(docs: Sequence[Document]) -> Operation[list[str]]:
    """Insert up to 200 prepared documents in one request, all or none."""

    def parse(payload: dict[str, Any]) -> list[str]:
        names = _message(payload)
        if not isinstance(names, list) or len(names) != len(docs):
            raise UnexpectedResponseError(
                "The reply does not list one name per document.", status_code=200
            )
        return [str(name) for name in names]

    return Operation(
        Request("POST", method_path("frappe.client.insert_many"), json={"docs": list(docs)}), parse
    )


def prepare_updates(
    doctype: str, updates: Mapping[str, Mapping[str, Any]]
) -> list[tuple[str, Document]]:
    """Validate ``updates`` and return ``(name, values)`` pairs in order.

    Raises:
        ConfigurationError: A name is empty, the values are not a mapping, or
            they try to set ``doctype``, ``docname`` or ``name``.
    """
    _check_doctype(doctype)
    if not isinstance(updates, Mapping):
        raise ConfigurationError("updates must map document names to the values to set.")
    prepared = []
    for name, values in updates.items():
        if not isinstance(name, str) or not name:
            raise ConfigurationError("Every document name in updates must be a non-empty string.")
        if not isinstance(values, Mapping):
            raise ConfigurationError(f"The values for {name!r} must be a mapping.")
        reserved = _UPDATE_RESERVED_KEYS.intersection(values)
        if reserved:
            raise ConfigurationError(
                f"The values for {name!r} must not set {', '.join(sorted(reserved))}; "
                "use rename() to rename a document."
            )
        prepared.append((name, dict(values)))
    return prepared


def _failure(entry: Any) -> BulkUpdateFailure:
    if not isinstance(entry, dict) or not isinstance(entry.get("doc"), dict):
        raise UnexpectedResponseError(
            "The reply lists a failure in an unknown form.", status_code=200
        )
    traceback = str(entry.get("exc") or "")
    lines = [line.strip() for line in traceback.splitlines() if line.strip()]
    return BulkUpdateFailure(
        name=str(entry["doc"].get("docname", "")),
        message=lines[-1] if lines else "The server did not say why.",
        traceback=traceback,
    )


def bulk_update(
    doctype: str, batch: Sequence[tuple[str, Document]]
) -> Operation[list[BulkUpdateFailure]]:
    """Update documents in one request; each is saved on its own."""
    docs = [{**values, "doctype": doctype, "docname": name} for name, values in batch]

    def parse(payload: dict[str, Any]) -> list[BulkUpdateFailure]:
        result = _message(payload)
        failed = result.get("failed_docs") if isinstance(result, dict) else None
        if not isinstance(failed, list):
            raise UnexpectedResponseError(
                "The reply does not contain failed_docs.", status_code=200
            )
        return [_failure(entry) for entry in failed]

    # Frappe's bulk_update parses ``docs`` with json.loads, so it must be a
    # JSON string even inside a JSON body.
    return Operation(
        Request("POST", method_path("frappe.client.bulk_update"), json={"docs": _dumps(docs)}),
        parse,
    )


def _scrub(label: str) -> str:
    """Turn a label into a field name the way ``frappe.scrub`` does."""
    return label.replace(" ", "_").replace("-", "_").lower()


def _report_column(column: Any) -> Document:
    """Normalise one column, as Frappe's ``get_column_as_dict`` does.

    Older reports describe columns as ``"Label:Fieldtype/Options:Width"``.
    """
    if isinstance(column, str):
        label, *rest = column.split(":")
        parsed: Document = {"label": label, "fieldname": _scrub(label)}
        if rest and rest[0]:
            fieldtype, _, options = rest[0].partition("/")
            parsed["fieldtype"] = fieldtype
            if options:
                parsed["options"] = options
        if len(rest) > 1 and rest[1]:
            parsed["width"] = rest[1]
        return parsed
    if isinstance(column, dict):
        parsed = dict(column)
        parsed.setdefault("label", str(parsed.get("fieldname") or ""))
        if not parsed.get("fieldname"):
            parsed["fieldname"] = _scrub(str(parsed["label"]))
        return parsed
    raise UnexpectedResponseError("The report lists a column in an unknown form.", status_code=200)


def _report_row(row: Any, fieldnames: Sequence[str]) -> Document:
    if isinstance(row, dict):
        return dict(row)
    if isinstance(row, list | tuple):
        return dict(zip(fieldnames, row, strict=False))
    raise UnexpectedResponseError("The report lists a row in an unknown form.", status_code=200)


def run_report(report_name: str, filters: Mapping[str, Any] | None) -> Operation[ReportResult]:
    """Run a Query, Script or Custom Report through ``frappe.desk.query_report.run``."""
    if not isinstance(report_name, str) or not report_name.strip():
        raise ConfigurationError("report_name must be a non-empty string.")
    if filters is not None and not isinstance(filters, Mapping):
        raise ConfigurationError("filters must be a mapping of filter names to values.")
    params = {"report_name": report_name, "filters": _dumps(dict(filters or {}))}
    if filters:
        # Without this, a Custom Report silently replaces the caller's
        # filters with the ones saved on the report.
        params["are_default_filters"] = "0"

    def parse(payload: dict[str, Any]) -> ReportResult:
        result = _message(payload)
        if not isinstance(result, dict):
            raise UnexpectedResponseError("The reply does not contain a report.", status_code=200)
        prepared = bool(result.get("prepared_report"))
        if prepared and "result" not in result:
            # A prepared report carries rows only once a background run for
            # these filters has completed.
            error = result.get("error")
            raise PreparedReportError(report_name, error=str(error) if error else None)
        raw_columns = result.get("columns") or []
        raw_rows = result.get("result") or []
        if not isinstance(raw_columns, list) or not isinstance(raw_rows, list):
            raise UnexpectedResponseError("The report reply is malformed.", status_code=200)
        columns = [_report_column(column) for column in raw_columns]
        fieldnames = [str(column["fieldname"]) for column in columns]
        rows = [_report_row(row, fieldnames) for row in raw_rows]
        # Frappe appends the totals row itself when the report asks for one.
        total_row = rows.pop() if result.get("add_total_row") and rows else None
        return ReportResult(
            columns=columns,
            rows=rows,
            total_row=total_row,
            message=result.get("message"),
            prepared=prepared,
            raw=result,
        )

    return Operation(
        Request("GET", method_path("frappe.desk.query_report.run"), params=params), parse
    )


PDF_CONTENT_TYPE = "application/pdf"


def pdf_request(
    doctype: str,
    name: str,
    *,
    print_format: str | None,
    letterhead: str | None,
    no_letterhead: bool,
    language: str | None,
) -> Request:
    """Describe a request for a document's print format as a PDF."""
    _check_doctype(doctype)
    if not isinstance(name, str) or not name:
        raise ConfigurationError("name must be a non-empty string.")
    if no_letterhead and letterhead is not None:
        raise ConfigurationError("Pass either letterhead or no_letterhead=True, not both.")
    params = {"doctype": doctype, "name": name, "no_letterhead": "1" if no_letterhead else "0"}
    optional = {"format": print_format, "letterhead": letterhead, "language": language}
    params.update({key: value for key, value in optional.items() if value is not None})
    return Request("GET", method_path("frappe.utils.print_format.download_pdf"), params=params)


# Field names keyset paging can request safely: plain columns, or every field.
_PLAIN_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_KEYSET_FIELDS = ("name", "creation")


def _as_filter_list(filters: Filters | None) -> list[Any]:
    """Return ``filters`` as a list, the way Frappe's ``make_filter_tuple`` reads a dict."""
    if filters is None:
        return []
    if isinstance(filters, Mapping):
        return [
            [key, value[0], value[1]] if isinstance(value, list | tuple) else [key, "=", value]
            for key, value in filters.items()
        ]
    return [list(condition) for condition in filters]


class ListPager:
    """Plans the requests of ``iter_list`` and filters their rows.

    With the default order, plain field names and no ``or_filters`` it pages
    by *keyset*: each request asks for the rows after the last one returned
    in ``(creation, name)`` order, written as Frappe filters::

        filters:    <caller's filters> AND creation >= X
        or_filters: creation > X OR name > Y

    which is ``creation > X OR (creation = X AND name > Y)``. The database
    seeks instead of skipping ``offset`` rows, so every page costs the same
    however deep the iteration goes and however many rows share a creation
    timestamp, and names are compared by the database itself, so the result
    always matches its sort order. Any other order, computed or child-table
    fields, or caller ``or_filters`` (which cannot be combined with these)
    use offset paging, which gives the same rows as before.

    The pager does no I/O: :meth:`next_operation` describes the next request
    and :meth:`accept` takes its rows, so both clients share it.
    """

    def __init__(
        self,
        doctype: str,
        *,
        fields: Sequence[str] | None,
        filters: Filters | None,
        or_filters: Filters | None,
        order_by: str | None,
        page_size: int,
        parent: str | None,
    ) -> None:
        """Validate the arguments and choose keyset or offset paging."""
        if isinstance(page_size, bool) or page_size < 1:
            raise ConfigurationError("page_size must be at least 1.")
        if isinstance(fields, str):
            raise ConfigurationError("fields must be a list of field names, not a string.")
        self._doctype = doctype
        self._or_filters = or_filters
        self._parent = parent
        self._page_size = page_size
        self.done = False
        self._keyset = (
            order_by is None
            and not or_filters
            and (fields is None or all(f == "*" or _PLAIN_FIELD.fullmatch(f) for f in fields))
        )
        if self._keyset:
            # Frappe returns name when no fields are asked for; so do we.
            wanted = list(fields) if fields else ["name"]
            every = "*" in wanted
            self._added = [] if every else [f for f in _KEYSET_FIELDS if f not in wanted]
            self._fields: list[str] | None = wanted + self._added
            self._filters: Filters | None = _as_filter_list(filters)
            self._order_by = STABLE_ORDER
        else:
            self._added = []
            self._fields = list(fields) if fields is not None else None
            self._filters = filters
            self._order_by = order_by or STABLE_ORDER
        self._offset = 0
        self._returned = 0
        self._after: tuple[Any, Any] | None = None

    @property
    def keyset(self) -> bool:
        """Whether this iteration pages by keyset rather than by offset."""
        return self._keyset

    def next_operation(self) -> Operation[list[Document]]:
        """Describe the request for the next page."""
        if not self._keyset:
            return get_list(
                self._doctype,
                fields=self._fields,
                filters=self._filters,
                or_filters=self._or_filters,
                order_by=self._order_by,
                limit=self._page_size,
                offset=self._offset,
                parent=self._parent,
            )
        filters = list(self._filters or [])
        or_filters: list[Any] | None = None
        if self._after is not None:
            creation, name = self._after
            filters.append(["creation", ">=", creation])
            or_filters = [["creation", ">", creation], ["name", ">", name]]
        return get_list(
            self._doctype,
            fields=self._fields,
            filters=filters,
            or_filters=or_filters,
            order_by=self._order_by,
            limit=self._page_size,
            offset=0,
            parent=self._parent,
        )

    def accept(self, rows: list[Document]) -> list[Document]:
        """Take a page's rows and return the ones to hand to the caller."""
        self.done = len(rows) < self._page_size
        self._returned += len(rows)
        if not self._keyset:
            self._offset += self._page_size
        elif rows:
            last = rows[-1]
            creation, name = last.get("creation"), last.get("name")
            if name is None:
                raise UnexpectedResponseError(
                    "A listed row has no name, so paging cannot continue.", status_code=200
                )
            if creation is None:
                # Rows written outside Frappe can lack a creation timestamp,
                # and no keyset seeks past them. They sort first, so every
                # row returned so far is a prefix of the same order: continue
                # from there by offset.
                self._keyset = False
                self._offset = self._returned
            elif (creation, name) == self._after:
                # The site ignored the keyset filters; stop rather than loop.
                raise UnexpectedResponseError(
                    "The site returned the same rows again, so paging cannot continue.",
                    status_code=200,
                )
            else:
                self._after = (creation, name)
        if self._added:
            rows = [{k: v for k, v in row.items() if k not in self._added} for row in rows]
        return rows
