"""Fail-closed GitHub API access for incident publication."""

import contextlib
import http.client
import json
import re
import unicodedata
from collections.abc import Callable
from typing import NoReturn, Protocol, cast
from urllib.parse import urlencode, urlsplit

from .incident_reporting import (
    Category,
    Failure,
    IncidentError,
    PublicExceptionClass,
    Report,
    Stage,
    Transport,
    _scan_sink,
    _summary,
    public_json,
)

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


_OWNER_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
_REPOSITORY_PATTERN = re.compile(rf"^{_OWNER_PATTERN}/[A-Za-z0-9._-]{{1,100}}$")
_MARKER_TEXT = r"<!-- ai-employee-incident:[0-9a-f]{64} -->"
_MARKER_PATTERN = re.compile(rf"^{_MARKER_TEXT}$")
_CATEGORY_VALUES = "|".join(re.escape(value.value) for value in Category)
_FAILURE_VALUES = "|".join(re.escape(value.value) for value in Failure)
_EXCEPTION_VALUES = "|".join(re.escape(value.value) for value in PublicExceptionClass)
_STAGE_VALUES = "|".join(re.escape(value.value) for value in Stage)
_TITLE_PATTERN = re.compile(
    rf"^\[incident\] (?P<category>{_CATEGORY_VALUES}): "
    rf"(?:{_FAILURE_VALUES}) "
    rf"\((?:{_EXCEPTION_VALUES})\) at (?:{_STAGE_VALUES})$"
)
_SUMMARY_PATTERN = re.compile(
    rf"^Occurrences: (?P<count>[1-9][0-9]{{0,2}}) of 999; "
    rf"category=(?:{_CATEGORY_VALUES}); failure=(?:{_FAILURE_VALUES}); "
    rf"exception_class=(?:{_EXCEPTION_VALUES}); stage=(?:{_STAGE_VALUES})$"
)
_BODY_PREFIX = "## Sanitized incident report\n\n"


def _require_repository(value: object) -> str:
    if (
        not isinstance(value, str)
        or _REPOSITORY_PATTERN.fullmatch(value) is None
        or ".." in value.split("/", 1)[1]
    ):
        raise IncidentError("INVALID_REPOSITORY")
    return value


def _require_issue_number(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise IncidentError(code)
    return value


def _public_text(
    value: object,
    limit: int,
    code: str,
    *,
    allow_line_feed: bool = False,
) -> str:
    if not isinstance(value, str):
        raise IncidentError(code)
    if any(
        unicodedata.category(character) == "Cc" and (not allow_line_feed or character != "\n")
        for character in value
    ):
        raise IncidentError(code)
    try:
        _scan_sink(value, limit)
    except IncidentError:
        raise IncidentError(code) from None
    return value


class GitHubIssuesTransport(Transport):
    """Fail-closed GitHub Issues implementation of the incident transport."""

    def __init__(self, requester: GitHubApiRequester) -> None:
        self._requester = requester

    def find_issue_by_marker(self, repository: str, marker: str) -> tuple[int, str] | None:
        repository = _require_repository(repository)
        if not isinstance(marker, str) or _MARKER_PATTERN.fullmatch(marker) is None:
            raise IncidentError("INVALID_MARKER")
        query = f'repo:{repository} is:issue "{marker}"'
        path = f"/search/issues?{urlencode({'q': query, 'per_page': '100'})}"
        response = self._requester.request("GET", path)
        items = response.get("items") if isinstance(response, dict) else None
        if not isinstance(items, list) or len(items) > 100:
            raise IncidentError("INVALID_SEARCH_RESPONSE")

        matches: list[tuple[int, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            body = item.get("body")
            if not isinstance(body, str) or marker not in body:
                continue
            number = _require_issue_number(item.get("number"), "INVALID_SEARCH_RESPONSE")
            url = f"https://github.com/{repository}/issues/{number}"
            if item.get("html_url") != url:
                raise IncidentError("INVALID_SEARCH_RESPONSE")
            matches.append((number, url))
        return min(matches) if matches else None

    def create_issue(
        self, repository: str, title: str, body: str, labels: tuple[str, ...]
    ) -> tuple[int, str]:
        repository = _require_repository(repository)
        title = _public_text(title, 256, "INVALID_TITLE")
        title_match = _TITLE_PATTERN.fullmatch(title)
        if title_match is None:
            raise IncidentError("INVALID_TITLE")
        body = _public_text(body, 4_096, "INVALID_BODY", allow_line_feed=True)
        markers = re.findall(_MARKER_TEXT, body)
        sections = body.split("\n\n")
        if (
            len(sections) != 4
            or sections[0] != _BODY_PREFIX.rstrip("\n")
            or len(markers) != 1
            or sections[3] != markers[0]
        ):
            raise IncidentError("INVALID_BODY")
        try:
            report = Report.model_validate_json(sections[2], strict=True)
            if public_json(report) != sections[2] or _summary(report) != sections[1]:
                raise ValueError
        except Exception:
            raise IncidentError("INVALID_BODY") from None
        expected_title = (
            f"[incident] {report.category.value}: {report.failure.value} "
            f"({report.exception_class.value}) at {report.stage.value}"
        )
        if title != expected_title:
            raise IncidentError("INVALID_TITLE")
        category = report.category.value
        expected_labels = (
            "ai-employee-incident",
            f"incident:{category}",
        )
        if labels != expected_labels:
            raise IncidentError("INVALID_LABELS")
        for label in labels:
            _public_text(label, 64, "INVALID_LABELS")

        response = self._requester.request(
            "POST",
            f"/repos/{repository}/issues",
            {"title": title, "body": body, "labels": list(labels)},
        )
        return self._receipt(response, repository)

    def update_occurrence_summary(self, repository: str, issue_number: int, summary: str) -> None:
        repository = _require_repository(repository)
        issue_number = _require_issue_number(issue_number, "INVALID_ISSUE_NUMBER")
        summary = _public_text(summary, 256, "INVALID_SUMMARY")
        if _SUMMARY_PATTERN.fullmatch(summary) is None:
            raise IncidentError("INVALID_SUMMARY")
        self._requester.request(
            "POST",
            f"/repos/{repository}/issues/{issue_number}/comments",
            {"body": summary},
        )

    @staticmethod
    def _receipt(response: object, repository: str) -> tuple[int, str]:
        if not isinstance(response, dict):
            raise IncidentError("INVALID_RECEIPT")
        number = _require_issue_number(response.get("number"), "INVALID_RECEIPT")
        url = f"https://github.com/{repository}/issues/{number}"
        if response.get("html_url") != url:
            raise IncidentError("INVALID_RECEIPT")
        return number, url
