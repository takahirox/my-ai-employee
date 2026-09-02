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
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class Transport(Protocol):
    def create_issue(
        self,
        repository: str,
        title: str,
        body: str,
        labels: tuple[str, ...],
    ) -> tuple[int, str]: ...


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, tuple[str, ...]]] = []

    def create_issue(
        self,
        repository: str,
        title: str,
        body: str,
        labels: tuple[str, ...],
    ) -> tuple[int, str]:
        self.calls.append((repository, title, body, labels))
        return 1, f"https://github.com/{repository}/issues/1"


class Outbox:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS incidents("
            "fingerprint TEXT PRIMARY KEY,body TEXT,digest TEXT,status TEXT,count INTEGER,"
            "expires TEXT,approval TEXT,approval_expires TEXT,receipt TEXT)"
        )
        self.db.commit()
        os.chmod(self.path, 0o600)

    def enqueue(self, report: Report, policy: Policy, now: datetime) -> sqlite3.Row | None:
        body = public_json(report)
        digest = canonical_digest(report.model_dump(mode="json"))
        expiry = (now.astimezone(UTC) + timedelta(hours=policy.retention_hours)).isoformat()
        row = self.db.execute(
            "SELECT * FROM incidents WHERE fingerprint=?",
            (report.fingerprint,),
        ).fetchone()
        if row:
            if row["status"] == "published":
                return cast(sqlite3.Row, row)
            report = report.model_copy(update={"occurrences": min(row["count"] + 1, 999)})
            body = public_json(report)
            digest = canonical_digest(report.model_dump(mode="json"))
            self.db.execute(
                "UPDATE incidents SET body=?,digest=?,status='pending',count=?,"
                "approval=NULL,approval_expires=NULL WHERE fingerprint=?",
                (body, digest, report.occurrences, report.fingerprint),
            )
        else:
            if (
                self.db.execute(
                    "SELECT count(*) FROM incidents WHERE status!='published'"
                ).fetchone()[0]
                >= policy.pending_cap
            ):
                raise IncidentError("OUTBOX_CAP")
            self.db.execute(
                "INSERT INTO incidents VALUES(?,?,?,'pending',1,?,NULL,NULL,NULL)",
                (report.fingerprint, body, digest, expiry),
            )
        self.db.commit()
        return cast(
            sqlite3.Row | None,
            self.db.execute(
                "SELECT * FROM incidents WHERE fingerprint=?",
                (report.fingerprint,),
            ).fetchone(),
        )
