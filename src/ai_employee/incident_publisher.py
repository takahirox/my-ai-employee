"""Fail-closed GitHub API access for incident publication."""

import contextlib
import http.client
import json
import unicodedata
from collections.abc import Callable
from typing import NoReturn, Protocol, cast
from urllib.parse import urlsplit

from .incident_reporting import IncidentError

_GITHUB_API_HOST = "api.github.com"
_GITHUB_API_TIMEOUT = 10
_MAX_PATH_LENGTH = 2048
_MAX_TOKEN_LENGTH = 4096
_MAX_PAYLOAD_BYTES = 16 * 1024
_MAX_RESPONSE_BYTES = 256 * 1024
_BASE_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "my-ai-employee",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GitHubApiRequester(Protocol):
    """Minimal GitHub JSON request interface used by publishers."""

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]: ...


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _validate_token(token: str) -> None:
    if (
        not isinstance(token, str)
        or not token
        or len(token) > _MAX_TOKEN_LENGTH
        or _contains_control(token)
    ):
        raise IncidentError("INVALID_TOKEN")


def _validate_path(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or len(path) > _MAX_PATH_LENGTH
        or "\\" in path
        or _contains_control(path)
    ):
        raise IncidentError("INVALID_PATH")
    try:
        parsed = urlsplit(path)
    except ValueError:
        raise IncidentError("INVALID_PATH") from None
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise IncidentError("INVALID_PATH")


def _encode_payload(payload: dict[str, object] | None) -> bytes | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise IncidentError("INVALID_PAYLOAD")
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except Exception:
        raise IncidentError("INVALID_PAYLOAD") from None
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise IncidentError("PAYLOAD_TOO_LARGE")
    return encoded


def _reject_json_constant(_: str) -> NoReturn:
    raise ValueError


class GitHubApiClient:
    """Small, bounded GitHub REST API client with no retry or redirect behavior."""

    def __init__(
        self,
        token: str,
        *,
        connection_factory: Callable[..., http.client.HTTPSConnection] | None = None,
    ) -> None:
        _validate_token(token)
        self._token = token
        self._connection_factory = connection_factory or http.client.HTTPSConnection

    def __repr__(self) -> str:
        return "GitHubApiClient(token=<redacted>)"

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if method not in {"GET", "POST"}:
            raise IncidentError("INVALID_METHOD")
        _validate_path(path)
        body = _encode_payload(payload)

        headers = dict(_BASE_HEADERS)
        headers["Authorization"] = f"Bearer {self._token}"
        if body is not None:
            headers["Content-Type"] = "application/json"

        connection: http.client.HTTPSConnection | None = None
        try:
            connection = self._connection_factory(
                _GITHUB_API_HOST,
                timeout=_GITHUB_API_TIMEOUT,
            )
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise IncidentError("HTTP_STATUS")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if not isinstance(raw, bytes):
                raise IncidentError("INVALID_RESPONSE")
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise IncidentError("RESPONSE_TOO_LARGE")
        except IncidentError:
            raise
        except Exception:
            raise IncidentError("REQUEST_FAILED") from None
        finally:
            if connection is not None:
                with contextlib.suppress(Exception):
                    connection.close()

        if not raw:
            raise IncidentError("EMPTY_RESPONSE")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise IncidentError("INVALID_UTF8") from None
        try:
            parsed = json.loads(text, parse_constant=_reject_json_constant)
        except (ValueError, RecursionError):
            raise IncidentError("INVALID_JSON") from None
        if not isinstance(parsed, dict):
            raise IncidentError("INVALID_RESPONSE")
        return cast(dict[str, object], parsed)
