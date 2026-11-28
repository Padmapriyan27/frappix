"""Security hardening from the 0.3.0 audit."""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import math
import ssl
import time
import traceback
import zlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from frappex_client import (
    AsyncClient,
    AuthenticationError,
    Client,
    ConfigurationError,
    PasswordAuth,
    RequestTimeoutError,
    RetryPolicy,
    TransportError,
    UnexpectedResponseError,
)
from frappex_client._protocol import Request, file_path

URL = "https://erp.example.com"
ONE_ATTEMPT = RetryPolicy(max_attempts=1)


class TestDownloadsStayInFileLocations:
    @pytest.mark.parametrize(
        "url",
        [
            "/api/resource/User/Administrator",
            "/api/method/logout",
            "/app/user",
            "/assets/frappe/js/x.js",
            "/files",
            "/filesystem/a.pdf",
            "/private/filesx/a.pdf",
        ],
    )
    def test_other_routes_are_refused(self, url: str) -> None:
        with pytest.raises(ConfigurationError, match="/files/"):
            file_path(url)

    @pytest.mark.parametrize(
        ("url", "path"),
        [
            ("/files/logo.png", "/files/logo.png"),
            ("/private/files/a b.pdf", "/private/files/a%20b.pdf"),
            ("/files/sub/dir/x.csv", "/files/sub/dir/x.csv"),
        ],
    )
    def test_file_locations_are_accepted(self, url: str, path: str) -> None:
        assert file_path(url) == path

    def test_nothing_is_sent_for_a_refused_route(self) -> None:
        sent: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        with Client(URL, transport=transport) as client, pytest.raises(ConfigurationError):
            client.download("/api/resource/User/Administrator")
        assert sent == []


class TestBodiesStayOutOfReprs:
    def test_request_repr_hides_the_body(self) -> None:
        request = Request("POST", "/api/method/login", json={"usr": "ann", "pwd": "hunter2"})
        assert "hunter2" not in repr(request)
        assert "/api/method/login" in repr(request)

    def test_a_failed_login_leaves_no_password_in_traceback_locals(self) -> None:
        transport = httpx.MockTransport(lambda r: httpx.Response(401, json={}))
        auth = PasswordAuth("ann@example.com", "hunter2-very-secret")
        with Client(URL, auth, transport=transport) as client:
            try:
                client.get_doc("ToDo", "T-1")
            except AuthenticationError as error:
                # What error trackers record: every frame's local variables.
                rendered = "".join(
                    traceback.TracebackException.from_exception(error, capture_locals=True).format()
                )
            else:
                pytest.fail("login should have failed")
        assert "hunter2-very-secret" not in rendered


class TestLoginFailuresLeakNoPassword:
    @pytest.mark.parametrize(
        "failure",
        [
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("slow"),
            httpx.RemoteProtocolError("bad"),
        ],
    )
    def test_a_network_failure_during_login_leaves_no_password_anywhere(
        self, failure: Exception
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise failure

        auth = PasswordAuth("ann@example.com", "hunter2-very-secret")
        with Client(URL, auth, transport=httpx.MockTransport(handler), retry=ONE_ATTEMPT) as client:
            try:
                client.get_doc("ToDo", "T-1")
            except TransportError as error:
                caught = error
            else:
                pytest.fail("login should have failed")
        rendered = "".join(
            traceback.TracebackException.from_exception(caught, capture_locals=True).format()
        )
        assert "hunter2-very-secret" not in rendered
        assert caught.__cause__ is None
        assert caught.__context__ is None


class TestTlsContexts:
    def test_a_context_that_does_not_verify_is_refused(self) -> None:
        with pytest.raises(ConfigurationError, match="CERT_REQUIRED"):
            # Deliberately unverified: the test proves the client refuses it.
            Client(URL, verify=ssl._create_unverified_context())  # noqa: S323

    def test_a_context_without_hostname_checks_is_refused(self) -> None:
        context = ssl.create_default_context()
        context.check_hostname = False
        with pytest.raises(ConfigurationError, match="hostnames"):
            AsyncClient(URL, verify=context)

    def test_a_verifying_context_is_accepted(self) -> None:
        Client(URL, verify=ssl.create_default_context()).close()


class TestCompression:
    def test_only_modest_encodings_are_offered(self) -> None:
        seen: dict[str, str | None] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["accept-encoding"] = request.headers.get("accept-encoding")
            return httpx.Response(200, json={"message": "pong"})

        with Client(URL, transport=httpx.MockTransport(handler)) as client:
            client.ping()
        assert seen["accept-encoding"] == "gzip, deflate"

    def test_the_encoding_cannot_be_overridden(self) -> None:
        with pytest.raises(ConfigurationError, match="Accept-Encoding"):
            Client(URL, headers={"Accept-Encoding": "br"})


def drip(chunks: int, pause: float) -> Iterator[bytes]:
    for _ in range(chunks):
        time.sleep(pause)
        yield b" "


async def async_drip(chunks: int, pause: float) -> AsyncIterator[bytes]:
    for _ in range(chunks):
        await asyncio.sleep(pause)
        yield b" "


class TestRequestDeadline:
    def test_a_slow_drip_reply_is_stopped(self) -> None:
        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=drip(100, 0.02)))
        started = time.monotonic()
        with (
            Client(URL, transport=transport, max_request_time=0.1, retry=ONE_ATTEMPT) as client,
            pytest.raises(RequestTimeoutError, match="max_request_time"),
        ):
            client.ping()
        assert time.monotonic() - started < 1

    def test_a_slow_drip_download_is_stopped(self) -> None:
        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=drip(100, 0.02)))
        with (
            Client(URL, transport=transport, max_request_time=0.1) as client,
            pytest.raises(RequestTimeoutError),
        ):
            client.download("/files/a.bin")

    def test_async_slow_drip_and_slow_headers_are_stopped(self) -> None:
        async def slow_headers(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(200, json={"message": "pong"})

        async def main() -> None:
            dripping = httpx.MockTransport(
                lambda r: httpx.Response(200, content=async_drip(100, 0.02))
            )
            async with AsyncClient(
                URL, transport=dripping, max_request_time=0.1, retry=ONE_ATTEMPT
            ) as client:
                with pytest.raises(RequestTimeoutError):
                    await client.ping()
                with pytest.raises(RequestTimeoutError):
                    await client.download("/files/a.bin")
            async with AsyncClient(
                URL,
                transport=httpx.MockTransport(slow_headers),
                max_request_time=0.1,
                retry=ONE_ATTEMPT,
            ) as client:
                with pytest.raises(RequestTimeoutError):
                    await client.ping()

        started = time.monotonic()
        asyncio.run(main())
        assert time.monotonic() - started < 2

    def test_a_reply_within_the_limit_is_fine(self) -> None:
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"message": "pong"}))
        with Client(URL, transport=transport, max_request_time=5) as client:
            assert client.ping() == "pong"


@pytest.mark.parametrize("kind", [Client, AsyncClient])
class TestDeadlineSetting:
    def test_defaults(self, kind: Any) -> None:
        assert kind(URL).max_request_time == 300
        assert kind(URL, timeout=600).max_request_time == 1200
        assert kind(URL, timeout=httpx.Timeout(10, read=400)).max_request_time == 800
        assert kind(URL, max_request_time=12.5).max_request_time == 12.5
        assert kind(URL, max_request_time=math.inf).max_request_time is None

    @pytest.mark.parametrize("value", [0, -1, True, "60", math.nan])
    def test_must_be_positive(self, kind: Any, value: Any) -> None:
        with pytest.raises(ConfigurationError, match="max_request_time"):
            kind(URL, max_request_time=value)


def untouchable() -> Iterator[bytes]:
    raise AssertionError("the body was read")
    yield b""  # pragma: no cover


class TestReplyEncodings:
    @pytest.mark.parametrize("encoding", ["br", "zstd", "gzip, br", "compress"])
    def test_an_encoding_not_offered_is_refused_before_reading(self, encoding: str) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=untouchable(), headers={"Content-Encoding": encoding}
            )

        transport = httpx.MockTransport(handler)
        with Client(URL, transport=transport, retry=ONE_ATTEMPT) as client:
            with pytest.raises(UnexpectedResponseError, match="Content-Encoding"):
                client.ping()
            with pytest.raises(UnexpectedResponseError, match="Content-Encoding"):
                client.download("/files/a.bin")

    def test_async_refuses_too(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x", headers={"Content-Encoding": "br"})

        async def main() -> None:
            async with AsyncClient(URL, transport=httpx.MockTransport(handler)) as client:
                with pytest.raises(UnexpectedResponseError, match="Content-Encoding"):
                    await client.ping()
                with pytest.raises(UnexpectedResponseError, match="Content-Encoding"):
                    await client.download_to("/files/a.bin", io.BytesIO())

        asyncio.run(main())

    @pytest.mark.parametrize("encoding", ["gzip", "deflate", "identity", "GZIP"])
    def test_offered_encodings_are_decoded(self, encoding: str) -> None:
        payload = json.dumps({"message": "pong"}).encode()
        body = {
            "gzip": gzip.compress(payload),
            "GZIP": gzip.compress(payload),
            "deflate": zlib.compress(payload),
            "identity": payload,
        }[encoding]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body, headers={"Content-Encoding": encoding})

        with Client(URL, transport=httpx.MockTransport(handler)) as client:
            assert client.ping() == "pong"
