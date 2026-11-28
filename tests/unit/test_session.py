"""Password sessions that expire on the site are renewed once."""

from __future__ import annotations

import asyncio
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest

from frappex_client import (
    AsyncClient,
    AuthenticationError,
    Client,
    PasswordAuth,
    PermissionDeniedError,
    TokenAuth,
)

URL = "https://erp.example.com"
AUTH = PasswordAuth("ann@example.com", "pw")


class Site:
    """A Frappe-like site with cookie sessions that the test can expire."""

    def __init__(self, *, forbidden: tuple[str, ...] = ()) -> None:
        self.valid_sid = ""
        self.logins = 0
        self.paths: list[str] = []
        self.forbidden = forbidden
        self._lock = threading.Lock()

    def expire(self) -> None:
        self.valid_sid = "expired-on-the-server"

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        with self._lock:
            self.paths.append(path)
            if path == "/api/method/login":
                self.logins += 1
                self.valid_sid = f"sid-{self.logins}"
                return httpx.Response(
                    200,
                    json={"message": "Logged In"},
                    headers={"Set-Cookie": f"sid={self.valid_sid}; Path=/"},
                )
            logged_in = f"sid={self.valid_sid}" in request.headers.get("cookie", "")
        if path == "/api/method/logout":
            return httpx.Response(200, json={})
        if path == "/api/method/ping":  # open to guests, as on Frappe
            return httpx.Response(200, json={"message": "pong"})
        if path == "/api/method/frappe.auth.get_logged_user":
            if not logged_in:
                return httpx.Response(403, json={"exc_type": "PermissionError"})
            return httpx.Response(200, json={"message": "ann@example.com"})
        if not logged_in or path in self.forbidden:
            return httpx.Response(403, json={"exc_type": "PermissionError"})
        if path.startswith("/files/"):
            return httpx.Response(200, content=b"file")
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"name": "NEW-1"}})
        return httpx.Response(200, json={"data": {"name": "T-1"}})


def client_for(site: Site, **options: Any) -> Client:
    return Client(
        URL, options.pop("auth", AUTH), transport=httpx.MockTransport(site.handle), **options
    )


class TestRenewal:
    def test_an_expired_session_is_renewed_and_the_call_repeated(self) -> None:
        site = Site()
        with client_for(site) as client:
            assert client.get_doc("ToDo", "T-1") == {"name": "T-1"}
            site.expire()
            assert client.get_doc("ToDo", "T-1") == {"name": "T-1"}
        assert site.logins == 2
        assert site.paths[1:6] == [
            "/api/resource/ToDo/T-1",
            "/api/resource/ToDo/T-1",
            "/api/method/frappe.auth.get_logged_user",
            "/api/method/login",
            "/api/resource/ToDo/T-1",
        ]

    def test_a_write_refused_as_logged_out_runs_once_on_the_new_session(self) -> None:
        site = Site()
        with client_for(site) as client:
            client.get_doc("ToDo", "T-1")
            site.expire()
            assert client.insert("ToDo", {"description": "x"}) == {"name": "NEW-1"}
        assert site.paths.count("/api/resource/ToDo") == 2  # refused, then done

    def test_a_real_permission_error_is_raised_without_logging_in_again(self) -> None:
        site = Site(forbidden=("/api/resource/Salary Slip/SS-1",))
        with client_for(site) as client, pytest.raises(PermissionDeniedError):
            client.get_doc("Salary Slip", "SS-1")
        assert site.logins == 1
        assert site.paths.count("/api/resource/Salary Slip/SS-1") == 1

    def test_renewal_happens_once_and_never_loops(self) -> None:
        site = Site()
        with client_for(site) as client:
            client.get_doc("ToDo", "T-1")
            site.forbidden = ("/api/resource/ToDo/T-1",)
            site.expire()
            with pytest.raises(PermissionDeniedError):
                client.get_doc("ToDo", "T-1")
        assert site.logins == 2

    def test_downloads_renew_too(self) -> None:
        site = Site()
        with client_for(site) as client:
            client.get_doc("ToDo", "T-1")
            site.expire()
            target = io.BytesIO()
            assert client.download_to("/files/a.txt", target) == 4
            assert target.getvalue() == b"file"
        assert site.logins == 2

    def test_many_threads_renew_the_session_once(self) -> None:
        site = Site()
        with client_for(site, max_concurrency=8) as client:
            client.get_doc("ToDo", "T-1")
            site.expire()
            with ThreadPoolExecutor(max_workers=8) as pool:
                docs = list(pool.map(lambda _: client.get_doc("ToDo", "T-1"), range(24)))
        assert docs == [{"name": "T-1"}] * 24
        assert site.logins == 2

    def test_token_auth_never_checks_the_session(self) -> None:
        site = Site()

        def handler(request: httpx.Request) -> httpx.Response:
            site.paths.append(request.url.path)
            return httpx.Response(403, json={"exc_type": "PermissionError"})

        with (
            Client(URL, TokenAuth("k", "s"), transport=httpx.MockTransport(handler)) as client,
            pytest.raises(PermissionDeniedError),
        ):
            client.get_doc("ToDo", "T-1")
        assert site.paths == ["/api/resource/ToDo/T-1"]

    def test_a_wrong_password_is_still_an_authentication_error(self) -> None:
        transport = httpx.MockTransport(lambda r: httpx.Response(401, json={}))
        with Client(URL, AUTH, transport=transport) as client, pytest.raises(AuthenticationError):
            client.ping()


class TestAsyncRenewal:
    def test_expired_session_and_concurrent_tasks(self) -> None:
        site = Site()

        async def main() -> list[Any]:
            transport = httpx.MockTransport(site.handle)
            async with AsyncClient(URL, AUTH, transport=transport) as client:
                await client.get_doc("ToDo", "T-1")
                site.expire()
                docs = await asyncio.gather(*(client.get_doc("ToDo", "T-1") for _ in range(20)))
                site.expire()
                target = io.BytesIO()
                await client.download_to("/files/a.txt", target)
                return [*docs, target.getvalue()]

        result = asyncio.run(main())
        assert result[:-1] == [{"name": "T-1"}] * 20
        assert result[-1] == b"file"
        assert site.logins == 3


class SlowSite(Site):
    """Logins take a while and replies arrive staggered, so 403s land mid-renewal."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        import time

        with self._lock:
            self.calls += 1
            delay = (self.calls % 5) * 0.01
        time.sleep(0.05 if request.url.path == "/api/method/login" else delay)
        return super().handle(request)

    async def handle_async(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.calls += 1
            delay = (self.calls % 5) * 0.01
        await asyncio.sleep(0.05 if request.url.path == "/api/method/login" else delay)
        return super().handle(request)


class TestRenewalRace:
    """Requests whose 403 arrives while another caller is logging in again."""

    def test_async_requests_landing_mid_renewal_all_succeed(self) -> None:
        for _ in range(5):
            site = SlowSite()

            async def main(site: SlowSite = site) -> list[Any]:
                transport = httpx.MockTransport(site.handle_async)
                async with AsyncClient(URL, AUTH, transport=transport) as client:
                    await client.get_doc("ToDo", "T-1")
                    site.expire()
                    return list(
                        await asyncio.gather(*(client.get_doc("ToDo", "T-1") for _ in range(10)))
                    )

            assert asyncio.run(main()) == [{"name": "T-1"}] * 10
            assert site.logins == 2

    def test_threads_landing_mid_renewal_all_succeed(self) -> None:
        for _ in range(3):
            site = SlowSite()
            with Client(URL, AUTH, transport=httpx.MockTransport(site.handle)) as client:
                client.get_doc("ToDo", "T-1")
                site.expire()
                with ThreadPoolExecutor(max_workers=10) as pool:
                    docs = list(pool.map(lambda _: client.get_doc("ToDo", "T-1"), range(10)))
            assert docs == [{"name": "T-1"}] * 10
            assert site.logins == 2
