"""Security guarantees, stated as properties and checked with generated input.

Each test names a promise made in SECURITY.md. Hypothesis searches for an
input that breaks it, so a regression here means a guarantee no longer holds.
"""

from __future__ import annotations

import contextlib
import json
import string
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from frappex_client import (
    AsyncClient,
    BearerAuth,
    Client,
    ConfigurationError,
    FrappexError,
    PasswordAuth,
    RetryPolicy,
    Secret,
    TokenAuth,
)
from frappex_client._config import normalise_base_url, validate_extra_headers
from frappex_client._protocol import RawResponse, decode, file_path, method_path, resource_path

BASE = "https://erp.example.com"
HOST = "erp.example.com"

names = st.text(min_size=1)
secrets = st.text(
    alphabet=string.ascii_letters + string.digits + "-_~+/=", min_size=16, max_size=64
)
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=12,
)


def request_for(path: str) -> httpx.Request:
    """Build the request exactly as the client would, without sending it."""
    with httpx.Client(base_url=BASE) as http:
        return http.build_request("GET", path)


class TestNamesCannotChangeTheEndpoint:
    @given(name=names)
    @example(name="../../method/frappe.client.delete")
    @example(name="a/../../b")
    @example(name="%2e%2e")
    @example(name="x?run_method=delete")
    @example(name="x#fragment")
    @example(name="\r\nHost: evil.example")
    def test_document_name_stays_one_path_segment(self, name: str) -> None:
        try:
            path = resource_path("ToDo", name)
        except ConfigurationError:
            assert name in {".", ".."}
            return
        prefix, segment = path.rsplit("/", 1)
        assert prefix == "/api/resource/ToDo"
        assert unquote(segment) == name
        assert not set(segment) & set("/?#\\\r\n ")

        request = request_for(path)
        assert request.url.host == HOST
        assert request.url.raw_path.decode() == path
        assert request.url.query == b""

    @given(doctype=names)
    def test_doctype_stays_one_path_segment(self, doctype: str) -> None:
        try:
            path = resource_path(doctype)
        except ConfigurationError:
            assert doctype in {".", ".."}
            return
        assert path.count("/") == 3
        assert request_for(path).url.raw_path.decode() == path

    @given(method=st.text(max_size=40))
    @example(method="frappe.client.get_count")
    @example(method="../resource/User")
    @example(method="frappe.client.get?x=1")
    def test_method_name_is_a_dotted_identifier_or_refused(self, method: str) -> None:
        try:
            path = method_path(method)
        except ConfigurationError:
            return
        assert path == f"/api/method/{method}"
        assert method.isascii()
        assert all(part.isidentifier() for part in method.split("."))


class TestDownloadsStayOnTheSite:
    @given(file_url=st.text(max_size=60))
    @example(file_url="https://evil.example/x")
    @example(file_url="//evil.example/x")
    @example(file_url="/\\evil.example/x")
    @example(file_url="/files/../../site_config.json")
    @example(file_url="/files/%2e%2e/secret")
    @example(file_url="/files/a b.pdf")
    def test_accepted_path_targets_the_configured_host(self, file_url: str) -> None:
        try:
            path = file_path(file_url)
        except ConfigurationError:
            return
        assert path.startswith("/")
        assert not path.startswith("//")
        assert all(unquote(part) not in {".", ".."} for part in path.split("/"))

        request = request_for(path)
        assert request.url.host == HOST
        assert request.url.scheme == "https"
        assert request.url.raw_path.decode() == path


class TestSecretsNeverPrint:
    @given(value=secrets)
    def test_secret_and_auth_objects_mask_the_value(self, value: str) -> None:
        shown = [
            repr(Secret(value)),
            str(Secret(value)),
            repr(TokenAuth("key", value)),
            repr(BearerAuth(value)),
            repr(PasswordAuth("user", value)),
        ]
        assert all(value not in text for text in shown)

    @given(value=secrets)
    def test_clients_mask_the_value(self, value: str) -> None:
        client = Client(BASE, TokenAuth("key", value))
        try:
            assert value not in repr(client)
            assert value not in str(vars(client).get("_auth"))
        finally:
            client.close()
        assert value not in repr(AsyncClient(BASE, BearerAuth(value)))

    @given(
        prefix=st.text(max_size=5), suffix=st.text(max_size=5), breaker=st.sampled_from("\r\n\x00")
    )
    def test_credentials_cannot_carry_a_header_break(
        self, prefix: str, suffix: str, breaker: str
    ) -> None:
        with pytest.raises(ConfigurationError):
            Secret(prefix + breaker + suffix)


class TestTransportIsNotDowngraded:
    @given(url=st.text(max_size=60))
    @example(url="http://erp.example.com")
    @example(url="HTTP://erp.example.com")
    @example(url="https://user:pw@erp.example.com")
    @example(url="javascript:alert(1)")
    @example(url="file:///etc/passwd")
    def test_only_clean_https_addresses_are_accepted(self, url: str) -> None:
        try:
            accepted = normalise_base_url(url, allow_insecure_http=False)
        except ConfigurationError:
            return
        assert accepted.startswith("https://")
        assert "@" not in accepted
        assert "?" not in accepted
        assert "#" not in accepted

    @given(
        name=st.text(min_size=1, max_size=10),
        value=st.text(max_size=10),
        breaker=st.sampled_from("\r\n\x00"),
    )
    def test_extra_headers_cannot_inject_lines(self, name: str, value: str, breaker: str) -> None:
        with pytest.raises(ConfigurationError):
            validate_extra_headers({name: value + breaker + "Injected: 1"})

    @pytest.mark.parametrize("kind", [Client, AsyncClient])
    def test_redirects_and_tls_bypass_have_no_switch(self, kind: Any) -> None:
        client = kind(BASE, TokenAuth("key", "secret-secret-secret"))
        assert client._http.follow_redirects is False
        for attempt in (False, True, 0, 1, b"ca.pem"):
            with pytest.raises(ConfigurationError):
                kind(BASE, TokenAuth("key", "secret-secret-secret"), verify=attempt)


class TestWritesAreNeverRepeated:
    @given(
        method=st.sampled_from(["POST", "PUT", "DELETE"]),
        attempt=st.integers(min_value=1, max_value=20),
        status=st.integers(min_value=500, max_value=599),
        retry_after=st.none() | st.floats(min_value=0, max_value=1e6),
        max_attempts=st.integers(min_value=1, max_value=20),
    )
    def test_no_retry_after_failure_or_5xx(
        self, method: str, attempt: int, status: int, retry_after: float | None, max_attempts: int
    ) -> None:
        policy = RetryPolicy(max_attempts=max_attempts)
        assert policy.delay_after_failure(method, attempt) is None
        assert policy.delay_after_response(method, attempt, status, retry_after) is None

    @given(
        method=st.sampled_from(["GET", "POST", "PUT", "DELETE"]),
        attempt=st.integers(min_value=1, max_value=50),
        status=st.integers(min_value=100, max_value=599),
        retry_after=st.none() | st.floats(min_value=0, max_value=1e9),
    )
    def test_retries_are_bounded_in_count_and_wait(
        self, method: str, attempt: int, status: int, retry_after: float | None
    ) -> None:
        policy = RetryPolicy(max_attempts=4, backoff_base=1, backoff_max=8, max_retry_after=30)
        for delay in (
            policy.delay_after_failure(method, attempt),
            policy.delay_after_response(method, attempt, status, retry_after),
        ):
            if attempt >= 4:
                assert delay is None
            if delay is not None:
                assert 0 <= delay <= 30


class TestHostileRepliesCannotCrashTheClient:
    @given(status=st.integers(min_value=100, max_value=599), body=st.binary(max_size=200))
    def test_arbitrary_bytes(self, status: int, body: bytes) -> None:
        with contextlib.suppress(FrappexError):
            assert isinstance(decode(RawResponse(status, {}, body)), dict)

    @given(
        status=st.integers(min_value=400, max_value=599),
        payload=st.fixed_dictionaries(
            {},
            optional={
                "exc_type": json_values,
                "exc": json_values,
                "_server_messages": json_values,
                "message": json_values,
            },
        ),
        retry_after=st.none() | st.text(max_size=12),
    )
    @example(status=417, payload={"_server_messages": '["\\ud800"]'}, retry_after=None)
    @example(status=500, payload={"exc": '"\\udfff"'}, retry_after="soon")
    @example(status=417, payload={"_server_messages": "[" * 5000}, retry_after=None)
    def test_arbitrary_error_documents(
        self, status: int, payload: dict[str, Any], retry_after: str | None
    ) -> None:
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        with pytest.raises(FrappexError) as caught:
            decode(RawResponse(status, headers, json.dumps(payload).encode()))
        assert isinstance(str(caught.value), str)


@pytest.mark.parametrize(
    ("call", "argument"),
    [
        (lambda v: resource_path(v), "To\ud9f0Do"),
        (lambda v: resource_path("ToDo", v), "T-\udc00"),
        (lambda v: file_path(v), "/files/\ud800.pdf"),
        (lambda v: method_path(v), "app.\ud800"),
    ],
)
def test_invalid_unicode_is_a_configuration_error(call: Any, argument: str) -> None:
    """Found by fuzzing: lone surrogates raised UnicodeEncodeError."""
    with pytest.raises(ConfigurationError):
        call(argument)
