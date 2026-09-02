import http.client
from typing import Any

import pytest

from ai_employee.incident_publisher import GitHubApiClient
from ai_employee.incident_reporting import IncidentError


class FakeResponse:
    def __init__(
        self,
        body: bytes = b'{"ok":true}',
        *,
        status: int = 200,
        read_error: Exception | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.read_error = read_error
        self.read_sizes: list[int] = []

    def read(self, amount: int) -> bytes:
        self.read_sizes.append(amount)
        if self.read_error is not None:
            raise self.read_error
        return self.body


class FakeConnection:
    def __init__(
        self,
        response: FakeResponse | None = None,
        *,
        request_error: Exception | None = None,
        response_error: Exception | None = None,
    ) -> None:
        self.response = response or FakeResponse()
        self.request_error = request_error
        self.response_error = response_error
        self.requests: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        self.closed = False

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        headers: dict[str, str],
    ) -> None:
        if self.request_error is not None:
            raise self.request_error
        self.requests.append((method, path, body, headers))

    def getresponse(self) -> FakeResponse:
        if self.response_error is not None:
            raise self.response_error
        return self.response

    def close(self) -> None:
        self.closed = True


class FakeFactory:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, *, timeout: int) -> FakeConnection:
        self.calls.append((host, timeout))
        return self.connection


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("real network access attempted")

    monkeypatch.setattr(http.client, "HTTPSConnection", fail_network)


def make_client(
    response: FakeResponse | None = None,
    *,
    request_error: Exception | None = None,
    response_error: Exception | None = None,
    token: str = "safe-token",
) -> tuple[GitHubApiClient, FakeConnection, FakeFactory]:
    connection = FakeConnection(
        response,
        request_error=request_error,
        response_error=response_error,
    )
    factory = FakeFactory(connection)
    client = GitHubApiClient(token, connection_factory=factory)
    return client, connection, factory


def test_post_uses_exact_endpoint_request_and_headers() -> None:
    response = FakeResponse(b'{"number":7}')
    client, connection, factory = make_client(response)

    result = client.request("POST", "/repos/o/r/issues", {"z": 2, "a": "é"})

    assert result == {"number": 7}
    assert type(result) is dict
    assert factory.calls == [("api.github.com", 10)]
    assert connection.requests == [
        (
            "POST",
            "/repos/o/r/issues",
            b'{"a":"\xc3\xa9","z":2}',
            {
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer safe-token",
                "Content-Type": "application/json",
                "User-Agent": "my-ai-employee",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    ]
    assert response.read_sizes == [256 * 1024 + 1]
    assert connection.closed


def test_patch_uses_exact_endpoint_request_and_headers() -> None:
    response = FakeResponse(b'{"number":7}')
    client, connection, factory = make_client(response)

    assert client.request("PATCH", "/repos/o/r/issues/7", {"title": "fixed"}) == {"number": 7}
    assert factory.calls == [("api.github.com", 10)]
    method, path, body, headers = connection.requests[0]
    assert (method, path, body) == (
        "PATCH",
        "/repos/o/r/issues/7",
        b'{"title":"fixed"}',
    )
    assert headers["Authorization"] == "Bearer safe-token"
    assert headers["Content-Type"] == "application/json"
    assert connection.closed


def test_get_sends_no_body_or_content_type() -> None:
    client, connection, _ = make_client()

    assert client.request("GET", "/repos/o/r/issues/1") == {"ok": True}

    method, path, body, headers = connection.requests[0]
    assert (method, path, body) == ("GET", "/repos/o/r/issues/1", None)
    assert "Content-Type" not in headers
    assert connection.closed


@pytest.mark.parametrize(
    "token",
    ["", "x" * 4097, "secret\rvalue", "secret\nvalue", "secret\x00value", "secret\x7f"],
)
def test_invalid_tokens_are_rejected_without_disclosure(token: str) -> None:
    with pytest.raises(IncidentError) as caught:
        GitHubApiClient(token)

    assert str(caught.value) == "INVALID_TOKEN"
    assert token not in repr(caught.value) or not token


def test_repr_and_upstream_error_redact_token() -> None:
    secret = "secret-token"
    client, connection, _ = make_client(
        request_error=RuntimeError(f"upstream leaked {secret}"),
        token=secret,
    )

    assert secret not in repr(client)
    with pytest.raises(IncidentError) as caught:
        client.request("GET", "/repos/o/r")
    assert str(caught.value) == "REQUEST_FAILED"
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert connection.closed


@pytest.mark.parametrize("status", [301, 307, 400, 404, 500])
def test_redirects_and_status_errors_fail_without_reading(status: int) -> None:
    response = FakeResponse(b"private upstream body", status=status)
    client, connection, _ = make_client(response)

    with pytest.raises(IncidentError, match=r"^HTTP_STATUS$"):
        client.request("GET", "/repos/o/r")

    assert response.read_sizes == []
    assert connection.closed


def test_oversized_response_fails_closed() -> None:
    response = FakeResponse(b"x" * (256 * 1024 + 1))
    client, connection, _ = make_client(response)

    with pytest.raises(IncidentError, match=r"^RESPONSE_TOO_LARGE$"):
        client.request("GET", "/repos/o/r")

    assert response.read_sizes == [256 * 1024 + 1]
    assert connection.closed


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"", "EMPTY_RESPONSE"),
        (b"not json", "INVALID_JSON"),
        (b'{"bad":NaN}', "INVALID_JSON"),
        (b"[]", "INVALID_RESPONSE"),
        (b'"text"', "INVALID_RESPONSE"),
        (b"\xff", "INVALID_UTF8"),
    ],
)
def test_invalid_response_bodies_fail_closed(body: bytes, code: str) -> None:
    client, connection, _ = make_client(FakeResponse(body))

    with pytest.raises(IncidentError, match=rf"^{code}$"):
        client.request("GET", "/repos/o/r")

    assert connection.closed


@pytest.mark.parametrize("stage", ["request", "response", "read", "parse"])
def test_connection_closes_for_every_failure_stage(stage: str) -> None:
    request_error = RuntimeError("request detail") if stage == "request" else None
    response_error = RuntimeError("response detail") if stage == "response" else None
    read_error = RuntimeError("read detail") if stage == "read" else None
    body = b"{" if stage == "parse" else b'{"ok":true}'
    response = FakeResponse(body, read_error=read_error)
    client, connection, _ = make_client(
        response,
        request_error=request_error,
        response_error=response_error,
    )

    expected = "INVALID_JSON" if stage == "parse" else "REQUEST_FAILED"
    with pytest.raises(IncidentError, match=rf"^{expected}$"):
        client.request("GET", "/repos/o/r")

    assert connection.closed


@pytest.mark.parametrize("method", ["get", "PUT", "DELETE", ""])
def test_invalid_method_is_rejected_before_connection(method: str) -> None:
    client, _, factory = make_client()

    with pytest.raises(IncidentError, match=r"^INVALID_METHOD$"):
        client.request(method, "/repos/o/r")

    assert factory.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "",
        "repos/o/r",
        "//evil.example/x",
        "https://evil.example",
        "/repos/o/r#fragment",
        "/bad\\path",
        "/bad\npath",
        "/bad\x7fpath",
        "/" + "x" * 2048,
    ],
)
def test_invalid_path_is_rejected_before_connection(path: str) -> None:
    client, _, factory = make_client()

    with pytest.raises(IncidentError, match=r"^INVALID_PATH$"):
        client.request("GET", path)

    assert factory.calls == []


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (["not", "an", "object"], "INVALID_PAYLOAD"),
        ({"bad": {1, 2}}, "INVALID_PAYLOAD"),
        ({"bad": float("nan")}, "INVALID_PAYLOAD"),
        ({"value": "x" * (16 * 1024)}, "PAYLOAD_TOO_LARGE"),
    ],
)
def test_invalid_payload_is_rejected_before_connection(payload: Any, code: str) -> None:
    client, _, factory = make_client()

    with pytest.raises(IncidentError, match=rf"^{code}$"):
        client.request("POST", "/repos/o/r/issues", payload)

    assert factory.calls == []
