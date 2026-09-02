"""Privacy-safe, opt-in incident Issue pipeline."""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, NamedTuple, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .serialization import canonical_digest, canonical_json, canonical_json_bytes


class IncidentError(RuntimeError):
    """Raised when an incident cannot cross the public reporting boundary."""


class Mode(StrEnum):
    OFF = "off"
    APPROVAL_REQUIRED = "approval_required"
    AUTO = "auto"


class Category(StrEnum):
    KERNEL = "trust_kernel_failure"
    STORAGE = "persistence_failure"
    WORKER = "worker_boundary_failure"


class TerminalState(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class Disposition(StrEnum):
    INTERNAL_PRODUCT_FAILURE = "internal_product_failure"
    USER_CODE_FAILURE = "user_code_failure"
    TEST_FAILURE = "test_failure"
    POLICY_DENIAL = "policy_denial"
    APPROVAL_WAIT = "approval_wait"
    INVALID_REQUEST = "invalid_request"
    EXPECTED_CANCELLATION = "expected_cancellation"


class Failure(StrEnum):
    ASSERTION = "assertion_error"
    OS = "os_error"
    RUNTIME = "runtime_error"
    TYPE = "type_error"
    VALUE = "value_error"


class Stage(StrEnum):
    RUNTIME = "runtime"
    STORAGE = "storage"
    POLICY = "policy"
    WORKER = "worker_boundary"


class Diagnosis(BaseModel):
    """Private incident diagnosis. Its detail must never enter a Report."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    category: Category
    terminal_state: TerminalState
    disposition: Disposition
    failure: Failure
    stage: Stage
    private_detail: str = Field(max_length=100_000)


_SEMVER_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_SYNTHETIC_REPRODUCTION: Literal["synthetic_reproduction_v1"] = "synthetic_reproduction_v1"


class Report(BaseModel):
    """The complete and deliberately small public incident schema."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"]
    category: Category
    terminal_state: TerminalState
    disposition: Disposition
    failure: Failure
    stage: Stage
    version: str = Field(min_length=5, max_length=128, pattern=_SEMVER_PATTERN)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    duration_bucket: int = Field(ge=0, le=3_600)
    memory_bucket: int = Field(ge=0, le=8_192)
    reproduction: Literal["synthetic_reproduction_v1"]
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurrences: int = Field(ge=1, le=999)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Mode = Mode.OFF
    repository: str | None = None
    auto_categories: tuple[Category, ...] = ()
    retention_hours: int = Field(default=168, ge=1, le=720)
    approval_hours: int = Field(default=24, ge=1, le=168)
    daily_limit: int = Field(default=3, ge=1, le=20)
    pending_cap: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def authority(self) -> Policy:
        if self.mode is not Mode.OFF and not self.repository:
            raise ValueError("repository required")
        if self.repository and not re.fullmatch(r"[\w.-]+/[\w.-]+", self.repository):
            raise ValueError("invalid repository")
        if self.mode is Mode.AUTO and not self.auto_categories:
            raise ValueError("auto allowlist required")
        return self


_PUBLIC_FIELDS = frozenset(Report.model_fields)
_MAX_PUBLIC_BYTES = 4_096
_SENSITIVE_TEXT = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:password|passwd|secret|credential|authorization|bearer|"
        r"api[_ -]?key|access[_ -]?token|refresh[_ -]?token)\b",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|"
        r"(?:AKIA|ASIA)[A-Z0-9]{16})\b",
        r"(?:https?|file|ssh)://",
        r"[/\\]",
        r"(?:^|[^a-z0-9])(?:canary|prompt|conversation|transcript|log|stdout|stderr|"
        r"stack(?:trace)?|message|diff|internal[_ -]?id)s?(?:$|[^a-z0-9])",
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-"
        r"[0-9a-f]{12}\b",
    )
)


def _bucket(value: float, buckets: tuple[int, ...]) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IncidentError("INVALID_METRIC")
    if not math.isfinite(value) or value < 0:
        raise IncidentError("INVALID_METRIC")
    return next((bucket for bucket in buckets if value <= bucket), buckets[-1])


def _scan_public(value: object, field: str | None = None) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or key not in _PUBLIC_FIELDS:
                raise IncidentError("PUBLIC_FIELD_DENIED")
            _scan_public(child, key)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _scan_public(child, field)
        return
    if isinstance(value, str):
        if field not in _PUBLIC_FIELDS or any(pattern.search(value) for pattern in _SENSITIVE_TEXT):
            raise IncidentError("PUBLIC_SCAN_DENIED")
        return
    if value is not None and not isinstance(value, (bool, int, float)):
        raise IncidentError("PUBLIC_TYPE_DENIED")


def public_json(report: Report) -> str:
    if not isinstance(report, Report) or set(report.__dict__) != _PUBLIC_FIELDS:
        raise IncidentError("PUBLIC_FIELD_DENIED")
    payload = report.model_dump(mode="json")
    _scan_public(payload)
    text = canonical_json(payload)
    if len(text.encode("utf-8")) > _MAX_PUBLIC_BYTES:
        raise IncidentError("PUBLIC_OUTPUT_TOO_LARGE")
    Report.model_validate_json(text, strict=True)
    return text


def qualifies_for_reporting(diagnosis: Diagnosis) -> bool:
    """Return whether a diagnosis is an unexpected internal terminal failure."""

    return (
        diagnosis.terminal_state is TerminalState.FAILED
        and diagnosis.disposition is Disposition.INTERNAL_PRODUCT_FAILURE
    )


def compose(
    diagnosis: Diagnosis,
    repository_key: bytes,
    version: str,
    commit: str,
    duration: float,
    memory: float,
) -> Report:
    if not qualifies_for_reporting(diagnosis):
        raise IncidentError("NOT_TERMINAL_INTERNAL")
    if not isinstance(repository_key, bytes) or len(repository_key) < 32:
        raise IncidentError("INVALID_KEY")

    stable = {
        "category": diagnosis.category.value,
        "terminal_state": diagnosis.terminal_state.value,
        "disposition": diagnosis.disposition.value,
        "failure": diagnosis.failure.value,
        "stage": diagnosis.stage.value,
        "version": version,
        "commit": commit,
    }
    fingerprint = hmac.new(
        repository_key,
        b"incident-fingerprint-v1\0" + canonical_json_bytes(stable),
        hashlib.sha256,
    ).hexdigest()
    report = Report(
        schema_version="1",
        category=diagnosis.category,
        terminal_state=diagnosis.terminal_state,
        disposition=diagnosis.disposition,
        failure=diagnosis.failure,
        stage=diagnosis.stage,
        version=version,
        commit=commit,
        duration_bucket=_bucket(duration, (0, 1, 5, 15, 30, 60, 300, 900, 3_600)),
        memory_bucket=_bucket(memory, (0, 64, 128, 256, 512, 1_024, 2_048, 4_096, 8_192)),
        reproduction=_SYNTHETIC_REPRODUCTION,
        fingerprint=fingerprint,
        occurrences=1,
    )
    public_json(report)
    return report


class RenderedIssue(NamedTuple):
    title: str
    body: str
    labels: tuple[str, ...]
    marker: str


class Preview(NamedTuple):
    digest: str
    report_digest: str
    issue: RenderedIssue


def _scan_sink(value: str, limit: int) -> None:
    if not value or len(value.encode()) > limit:
        raise IncidentError("PUBLIC_OUTPUT_TOO_LARGE")
    if any(pattern.search(value) for pattern in _SENSITIVE_TEXT):
        raise IncidentError("PUBLIC_SCAN_DENIED")


def _summary(report: Report) -> str:
    value = (
        f"Occurrences: {report.occurrences} of 999; category={report.category.value}; "
        f"failure={report.failure.value}; stage={report.stage.value}"
    )
    _scan_sink(value, 256)
    return value


def render_public_issue(report: Report, repository_key: bytes) -> RenderedIssue:
    payload = public_json(report)
    if not isinstance(repository_key, bytes) or len(repository_key) < 32:
        raise IncidentError("INVALID_KEY")
    stable = report.model_dump(mode="json")
    stable.pop("occurrences")
    digest = hmac.new(
        repository_key,
        b"incident-issue-marker-v1\0" + canonical_json_bytes(stable),
        hashlib.sha256,
    ).hexdigest()
    marker = f"<!-- ai-employee-incident:{digest} -->"
    title = f"[incident] {report.category.value}: {report.failure.value} at {report.stage.value}"
    labels = ("ai-employee-incident", f"incident:{report.category.value}")
    body = f"## Sanitized incident report\n\n{_summary(report)}\n\n{payload}\n\n{marker}"
    for value, limit in ((title, 256), (body, _MAX_PUBLIC_BYTES), (marker, 128)):
        _scan_sink(value, limit)
    for label in labels:
        _scan_sink(label, 64)
    return RenderedIssue(title, body, labels, marker)


class Transport(Protocol):
    def find_issue_by_marker(self, repository: str, marker: str) -> tuple[int, str] | None: ...

    def create_issue(
        self, repository: str, title: str, body: str, labels: tuple[str, ...]
    ) -> tuple[int, str]: ...

    def update_occurrence_summary(
        self, repository: str, issue_number: int, summary: str
    ) -> None: ...


class FakeTransport:
    def __init__(
        self,
        failures: Mapping[str, int | BaseException] | None = None,
        existing: Mapping[tuple[str, str], tuple[int, str]] | None = None,
    ) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.issues = dict(existing or {})
        self.failures = dict(failures or {})
        self.next_issue = 1

    def _call(self, name: str, *arguments: object) -> None:
        self.calls.append((name, *arguments))
        failure = self.failures.get(name)
        if isinstance(failure, int) and failure > 0:
            self.failures[name] = failure - 1
            raise IncidentError("TRANSPORT_FAILURE")
        if isinstance(failure, BaseException):
            raise failure

    def find_issue_by_marker(self, repository: str, marker: str) -> tuple[int, str] | None:
        self._call("find_issue_by_marker", repository, marker)
        return self.issues.get((repository, marker))

    def create_issue(
        self, repository: str, title: str, body: str, labels: tuple[str, ...]
    ) -> tuple[int, str]:
        self._call("create_issue", repository, title, body, labels)
        number = self.next_issue
        self.next_issue += 1
        url = f"https://github.com/{repository}/issues/{number}"
        match = re.search(r"<!-- ai-employee-incident:[0-9a-f]{64} -->", body)
        if match:
            self.issues[(repository, match.group())] = (number, url)
        return number, url

    def update_occurrence_summary(self, repository: str, issue_number: int, summary: str) -> None:
        self._call("update_occurrence_summary", repository, issue_number, summary)


_SCHEMA = (
    "CREATE TABLE incidents(repository TEXT NOT NULL,fingerprint TEXT NOT NULL,"
    "report_json TEXT NOT NULL CHECK(length(report_json)<=4096),"
    "report_digest TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN('pending','published')),"
    "occurrence_count INTEGER NOT NULL CHECK(occurrence_count BETWEEN 1 AND 999),"
    "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,expires_at TEXT NOT NULL,"
    "preview_digest TEXT,preview_report_digest TEXT,approval_digest TEXT,"
    "approval_expires_at TEXT,issue_number INTEGER,public_url TEXT,"
    "public_report_digest TEXT,authorization_mode TEXT,authorization_digest TEXT,"
    "authorized_at TEXT,published_at TEXT,PRIMARY KEY(repository,fingerprint));"
    "CREATE TABLE publication_log(repository TEXT NOT NULL,fingerprint TEXT NOT NULL,"
    "published_at TEXT NOT NULL,PRIMARY KEY(repository,fingerprint,published_at));"
)


def _utc(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise IncidentError("UTC_TIME_REQUIRED")
    return now.astimezone(UTC)


def _time(now: datetime) -> str:
    return _utc(now).isoformat(timespec="seconds")


def _digest(report: Report) -> str:
    return canonical_digest(report.model_dump(mode="json"))


def _authorization(policy: Policy, report_digest: str, preview_digest: str) -> str:
    return canonical_digest(
        {
            "mode": policy.mode.value,
            "repository": policy.repository,
            "report_digest": report_digest,
            "preview_digest": preview_digest,
        }
    )


class Outbox:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        parent = self.path.parent
        if parent.exists():
            if parent.is_symlink() or not parent.is_dir() or parent.stat().st_mode & 0o077:
                raise IncidentError("PRIVATE_PARENT_REQUIRED")
        else:
            parent.mkdir(mode=0o700, parents=True)
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise IncidentError("PRIVATE_DATABASE_REQUIRED")
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        if not self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='incidents'"
        ).fetchone():
            self.db.executescript(_SCHEMA)
            self.db.commit()
        os.chmod(self.path, 0o600)

    def __enter__(self) -> Outbox:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    def _repository(self, policy: Policy, report: Report) -> str:
        try:
            policy = Policy.model_validate(policy.model_dump(), strict=True)
        except ValidationError as error:
            raise IncidentError("POLICY_INVALID") from error
        if policy.mode is Mode.OFF:
            raise IncidentError("POLICY_OFF")
        if policy.repository is None:
            raise IncidentError("REPOSITORY_REQUIRED")
        for segment in policy.repository.split("/"):
            _scan_sink(segment, 100)
        if policy.mode is Mode.AUTO and report.category not in policy.auto_categories:
            raise IncidentError("AUTO_CATEGORY_DENIED")
        return policy.repository

    def _purge(self, now: datetime) -> int:
        deleted = self.db.execute(
            "DELETE FROM incidents WHERE expires_at<=?", (_time(now),)
        ).rowcount
        start = _utc(now).replace(hour=0, minute=0, second=0, microsecond=0)
        self.db.execute("DELETE FROM publication_log WHERE published_at<?", (_time(start),))
        return deleted

    def purge(self, now: datetime) -> int:
        with self.db:
            return self._purge(now)

    def enqueue(self, report: Report, policy: Policy, now: datetime) -> sqlite3.Row:
        repository = self._repository(policy, report)
        report = Report.model_validate_json(public_json(report), strict=True)
        timestamp = _time(now)
        expires = _time(_utc(now) + timedelta(hours=policy.retention_hours))
        with self.db:
            self._purge(now)
            row = self.db.execute(
                "SELECT * FROM incidents WHERE repository=? AND fingerprint=?",
                (repository, report.fingerprint),
            ).fetchone()
            if row is None:
                pending = self.db.execute(
                    "SELECT count(*) FROM incidents WHERE repository=? AND status='pending'",
                    (repository,),
                ).fetchone()[0]
                if cast(int, pending) >= policy.pending_cap:
                    raise IncidentError("OUTBOX_CAP")
                stored = report
                self.db.execute(
                    "INSERT INTO incidents(repository,fingerprint,report_json,report_digest,"
                    "status,occurrence_count,created_at,updated_at,expires_at)"
                    "VALUES(?,?,?,?,'pending',?,?,?,?)",
                    (
                        repository,
                        report.fingerprint,
                        public_json(stored),
                        _digest(stored),
                        stored.occurrences,
                        timestamp,
                        timestamp,
                        expires,
                    ),
                )
            else:
                count = min(cast(int, row["occurrence_count"]) + report.occurrences, 999)
                stored = report.model_copy(update={"occurrences": count})
                self.db.execute(
                    "UPDATE incidents SET report_json=?,report_digest=?,status='pending',"
                    "occurrence_count=?,updated_at=?,expires_at=?,preview_digest=NULL,"
                    "preview_report_digest=NULL,approval_digest=NULL,approval_expires_at=NULL,"
                    "public_report_digest=NULL,authorization_mode=NULL,"
                    "authorization_digest=NULL,authorized_at=NULL,published_at=NULL "
                    "WHERE repository=? AND fingerprint=?",
                    (
                        public_json(stored),
                        _digest(stored),
                        count,
                        timestamp,
                        expires,
                        repository,
                        report.fingerprint,
                    ),
                )
            result = self.db.execute(
                "SELECT * FROM incidents WHERE repository=? AND fingerprint=?",
                (repository, report.fingerprint),
            ).fetchone()
        if result is None:
            raise IncidentError("OUTBOX_WRITE_FAILED")
        return cast(sqlite3.Row, result)

    def _load(self, repository: str, fingerprint: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM incidents WHERE repository=? AND fingerprint=?",
            (repository, fingerprint),
        ).fetchone()
        if row is None:
            raise IncidentError("INCIDENT_NOT_FOUND")
        return cast(sqlite3.Row, row)

    def preview(
        self, fingerprint: str, policy: Policy, repository_key: bytes, now: datetime
    ) -> Preview:
        if policy.mode is Mode.OFF or policy.repository is None:
            raise IncidentError("POLICY_OFF")
        with self.db:
            self._purge(now)
            row = self._load(policy.repository, fingerprint)
            report = Report.model_validate_json(cast(str, row["report_json"]), strict=True)
            repository = self._repository(policy, report)
            issue = render_public_issue(report, repository_key)
            report_digest = _digest(report)
            preview_digest = canonical_digest(
                {
                    "repository": repository,
                    "report_digest": report_digest,
                    "title": issue.title,
                    "body": issue.body,
                    "labels": issue.labels,
                    "marker": issue.marker,
                }
            )
            self.db.execute(
                "UPDATE incidents SET preview_digest=?,preview_report_digest=? "
                "WHERE repository=? AND fingerprint=?",
                (preview_digest, report_digest, repository, fingerprint),
            )
        return Preview(preview_digest, report_digest, issue)

    def approve(self, fingerprint: str, preview_digest: str, policy: Policy, now: datetime) -> str:
        if policy.mode is not Mode.APPROVAL_REQUIRED or policy.repository is None:
            raise IncidentError("APPROVAL_NOT_ALLOWED")
        with self.db:
            self._purge(now)
            row = self._load(policy.repository, fingerprint)
            report = Report.model_validate_json(cast(str, row["report_json"]), strict=True)
            repository = self._repository(policy, report)
            report_digest = _digest(report)
            if (
                row["preview_digest"] != preview_digest
                or row["preview_report_digest"] != report_digest
            ):
                raise IncidentError("STALE_PREVIEW")
            approval = _authorization(policy, report_digest, preview_digest)
            expiry = min(
                _utc(now) + timedelta(hours=policy.approval_hours),
                datetime.fromisoformat(cast(str, row["expires_at"])).astimezone(UTC),
            )
            self.db.execute(
                "UPDATE incidents SET approval_digest=?,approval_expires_at=? "
                "WHERE repository=? AND fingerprint=?",
                (approval, _time(expiry), repository, fingerprint),
            )
        return approval

    def _authorize(
        self,
        row: sqlite3.Row,
        report: Report,
        policy: Policy,
        preview_digest: str,
        now: datetime,
    ) -> tuple[str, str]:
        repository = self._repository(policy, report)
        report_digest = _digest(report)
        if row["preview_digest"] != preview_digest or row["preview_report_digest"] != report_digest:
            raise IncidentError("STALE_PREVIEW")
        authorization = _authorization(policy, report_digest, preview_digest)
        if policy.mode is Mode.APPROVAL_REQUIRED:
            expires = cast(str | None, row["approval_expires_at"])
            if row["approval_digest"] != authorization:
                raise IncidentError("APPROVAL_REQUIRED")
            if expires is None or datetime.fromisoformat(expires) <= _utc(now):
                raise IncidentError("APPROVAL_EXPIRED")
        start = _time(_utc(now).replace(hour=0, minute=0, second=0, microsecond=0))
        count = self.db.execute(
            "SELECT count(*) FROM publication_log WHERE repository=? AND published_at>=?",
            (repository, start),
        ).fetchone()[0]
        if cast(int, count) >= policy.daily_limit:
            raise IncidentError("DAILY_LIMIT")
        return report_digest, authorization

    def publish(
        self,
        fingerprint: str,
        preview_digest: str,
        policy: Policy,
        repository_key: bytes,
        transport: Transport,
        now: datetime,
    ) -> tuple[int, str]:
        if policy.mode is Mode.OFF or policy.repository is None:
            raise IncidentError("POLICY_OFF")
        repository = policy.repository
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._purge(now)
            row = self._load(repository, fingerprint)
            report = Report.model_validate_json(cast(str, row["report_json"]), strict=True)
            issue = render_public_issue(report, repository_key)
            report_digest, authorization = self._authorize(row, report, policy, preview_digest, now)
            for value, limit in (
                (issue.title, 256),
                (issue.body, _MAX_PUBLIC_BYTES),
                (issue.marker, 128),
            ):
                _scan_sink(value, limit)
            for label in issue.labels:
                _scan_sink(label, 64)
            existing = transport.find_issue_by_marker(repository, issue.marker)
            report_digest, authorization = self._authorize(row, report, policy, preview_digest, now)
            if existing is None:
                number, url = transport.create_issue(
                    repository, issue.title, issue.body, issue.labels
                )
            else:
                number, url = existing
                summary = _summary(report)
                _scan_sink(summary, 256)
                transport.update_occurrence_summary(repository, number, summary)
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise IncidentError("INVALID_TRANSPORT_RECEIPT")
            if url != f"https://github.com/{repository}/issues/{number}":
                raise IncidentError("INVALID_TRANSPORT_RECEIPT")
            timestamp = _time(now)
            self.db.execute(
                "UPDATE incidents SET status='published',issue_number=?,public_url=?,"
                "public_report_digest=?,authorization_mode=?,authorization_digest=?,"
                "authorized_at=?,published_at=? WHERE repository=? AND fingerprint=?",
                (
                    number,
                    url,
                    report_digest,
                    policy.mode.value,
                    authorization,
                    timestamp,
                    timestamp,
                    repository,
                    fingerprint,
                ),
            )
            self.db.execute(
                "INSERT INTO publication_log(repository,fingerprint,published_at)VALUES(?,?,?)",
                (repository, fingerprint, timestamp),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return number, url
