"""Offline security tests for the deterministic GitHub Issues transport."""

from __future__ import annotations

from urllib.parse import urlencode

import pytest

from ai_employee.incident_publisher import GitHubIssuesTransport
from ai_employee.incident_reporting import (
    Category,
    Diagnosis,
    Disposition,
    Failure,
    IncidentError,
    PublicExceptionClass,
    Stage,
    TerminalState,
    compose,
    render_public_issue,
)

REPOSITORY = "owner/repository"
KEY = b"repository-key-32-bytes-minimum!"
CANARY = "private-canary-/tmp/secret-ghp_0123456789abcdefghijkl"
REPORT = compose(
    Diagnosis(
        category=Category.KERNEL,
        terminal_state=TerminalState.FAILED,
        disposition=Disposition.INTERNAL_PRODUCT_FAILURE,
        failure=Failure.RUNTIME,
        exception_class=PublicExceptionClass.RUNTIME_ERROR,
        stage=Stage.RUNTIME,
        private_detail=CANARY,
    ),
    KEY,
    "1.2.3",
    "a" * 40,
    5,
    64,
)
ISSUE = render_public_issue(REPORT, KEY)
SUMMARY = ISSUE.body.split("\n\n")[1]


class FakeRequester:
    def __init__(self, *responses: dict[str, object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append((method, path, payload))
        return self.responses.pop(0) if self.responses else {}


def test_search_is_exact_encoded_and_deduplicates_to_lowest_number() -> None:
    requester = FakeRequester(
        {
            "items": [
                {
                    "number": 9,
                    "html_url": f"https://github.com/{REPOSITORY}/issues/9",
                    "body": ISSUE.body,
                },
                {
                    "number": 2,
                    "html_url": f"https://github.com/{REPOSITORY}/issues/2",
                    "body": ISSUE.marker,
                },
                {"number": 1, "html_url": CANARY, "body": "unrelated public issue"},
            ]
        }
    )
    transport = GitHubIssuesTransport(requester)

    assert transport.find_issue_by_marker(REPOSITORY, ISSUE.marker) == (
        2,
        f"https://github.com/{REPOSITORY}/issues/2",
    )
    query = f'repo:{REPOSITORY} is:issue "{ISSUE.marker}"'
    assert requester.calls == [
        (
            "GET",
            f"/search/issues?{urlencode({'q': query, 'per_page': '100'})}",
            None,
        )
    ]


def test_search_rejects_a_malformed_matching_receipt() -> None:
    requester = FakeRequester({"items": [{"number": 7, "html_url": CANARY, "body": ISSUE.marker}]})
    with pytest.raises(IncidentError, match="INVALID_SEARCH_RESPONSE") as caught:
        GitHubIssuesTransport(requester).find_issue_by_marker(REPOSITORY, ISSUE.marker)
    assert CANARY not in str(caught.value)


def test_create_and_idempotent_update_have_exact_paths_and_payloads() -> None:
    url = f"https://github.com/{REPOSITORY}/issues/7"
    receipt = {"number": 7, "html_url": url}
    requester = FakeRequester(receipt, receipt)
    transport = GitHubIssuesTransport(requester)

    assert transport.create_issue(REPOSITORY, *ISSUE[:3]) == (7, url)
    assert transport.update_issue(REPOSITORY, 7, *ISSUE[:3]) == (7, url)

    assert requester.calls == [
        (
            "POST",
            f"/repos/{REPOSITORY}/issues",
            {"title": ISSUE.title, "body": ISSUE.body, "labels": list(ISSUE.labels)},
        ),
        (
            "PATCH",
            f"/repos/{REPOSITORY}/issues/7",
            {"title": ISSUE.title, "body": ISSUE.body, "labels": list(ISSUE.labels)},
        ),
    ]


@pytest.mark.parametrize(
    "repository",
    ("owner/..", "owner/a..b", "https://github.com/owner/repo", "owner/repo/path", "owner/repo?x"),
)
def test_invalid_repository_never_reaches_requester(repository: str) -> None:
    requester = FakeRequester()
    transport = GitHubIssuesTransport(requester)
    with pytest.raises(IncidentError, match="INVALID_REPOSITORY"):
        transport.find_issue_by_marker(repository, ISSUE.marker)
    with pytest.raises(IncidentError, match="INVALID_REPOSITORY"):
        transport.create_issue(repository, *ISSUE[:3])
    with pytest.raises(IncidentError, match="INVALID_REPOSITORY"):
        transport.update_issue(repository, 1, *ISSUE[:3])
    assert requester.calls == []


@pytest.mark.parametrize(
    "marker",
    ("", "<!-- ai-employee-incident:ABC -->", f"{ISSUE.marker}{ISSUE.marker}", CANARY),
)
def test_invalid_marker_never_searches(marker: str) -> None:
    requester = FakeRequester()
    with pytest.raises(IncidentError, match="INVALID_MARKER"):
        GitHubIssuesTransport(requester).find_issue_by_marker(REPOSITORY, marker)
    assert requester.calls == []


@pytest.mark.parametrize("operation", ("create", "update"))
@pytest.mark.parametrize(
    ("title", "body", "labels", "code"),
    (
        (CANARY, ISSUE.body, ISSUE.labels, "INVALID_TITLE"),
        (ISSUE.title, ISSUE.body + "\n" + CANARY, ISSUE.labels, "INVALID_BODY"),
        (ISSUE.title, ISSUE.body + ISSUE.marker, ISSUE.labels, "INVALID_BODY"),
        (ISSUE.title, ISSUE.body, ("ai-employee-incident",), "INVALID_LABELS"),
        (
            ISSUE.title.replace(Category.KERNEL.value, Category.WORKER.value),
            ISSUE.body,
            ("ai-employee-incident", f"incident:{Category.WORKER.value}"),
            "INVALID_TITLE",
        ),
    ),
)
def test_invalid_rendered_issue_never_posts(
    operation: str,
    title: str,
    body: str,
    labels: tuple[str, ...],
    code: str,
) -> None:
    requester = FakeRequester()
    with pytest.raises(IncidentError, match=code) as caught:
        transport = GitHubIssuesTransport(requester)
        if operation == "create":
            transport.create_issue(REPOSITORY, title, body, labels)
        else:
            transport.update_issue(REPOSITORY, 1, title, body, labels)
    assert CANARY not in str(caught.value)
    assert requester.calls == []


@pytest.mark.parametrize("number", (True, 0, -1, "1"))
def test_invalid_issue_number_never_posts(number: object) -> None:
    requester = FakeRequester()
    with pytest.raises(IncidentError, match="INVALID_ISSUE_NUMBER"):
        GitHubIssuesTransport(requester).update_issue(
            REPOSITORY,
            number,
            *ISSUE[:3],  # type: ignore[arg-type]
        )
    assert requester.calls == []


@pytest.mark.parametrize(
    "response",
    (
        {"number": True, "html_url": f"https://github.com/{REPOSITORY}/issues/1"},
        {"number": 1, "html_url": CANARY},
        {"number": 0, "html_url": f"https://github.com/{REPOSITORY}/issues/0"},
    ),
)
def test_invalid_create_receipt_fails_closed(response: dict[str, object]) -> None:
    requester = FakeRequester(response)
    with pytest.raises(IncidentError, match="INVALID_RECEIPT") as caught:
        GitHubIssuesTransport(requester).create_issue(REPOSITORY, *ISSUE[:3])
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    (
        {"number": 8, "html_url": f"https://github.com/{REPOSITORY}/issues/8"},
        {"number": 7, "html_url": CANARY},
    ),
)
def test_invalid_update_receipt_fails_closed(response: dict[str, object]) -> None:
    requester = FakeRequester(response)
    with pytest.raises(IncidentError) as caught:
        GitHubIssuesTransport(requester).update_issue(REPOSITORY, 7, *ISSUE[:3])
    assert CANARY not in str(caught.value)


def test_transport_exposes_only_the_protocol_operations() -> None:
    transport = GitHubIssuesTransport(FakeRequester())
    assert callable(transport.find_issue_by_marker)
    assert callable(transport.create_issue)
    assert callable(transport.update_issue)
    assert not hasattr(transport, "token")
    assert not hasattr(transport, "publish")
