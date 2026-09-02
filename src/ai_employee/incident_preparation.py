"""Fail-closed preparation of terminal internal incidents."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Never, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .incident_reporting import (
    Category,
    Diagnosis,
    Disposition,
    Failure,
    IncidentError,
    Mode,
    Outbox,
    Policy,
    PublicExceptionClass,
    Stage,
    TerminalState,
    compose,
    public_json,
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class InternalIncidentCode(StrEnum):
    DEADLINE_WATCHDOG_TIMEOUT = "deadline_watchdog_timeout"
    STRUCTURED_OUTPUT_MISSING = "structured_output_missing"
    ENVELOPE_INVALID = "envelope_invalid"
    WORKER_RESULT_ABSENT = "worker_result_absent"
    PROCESS_CLEANUP_FAILED = "process_cleanup_failed"
    DIAGNOSTIC_PERSISTENCE_FAILED = "diagnostic_persistence_failed"
    REPAIR_EXHAUSTED = "repair_exhausted"
    DIFF_HUNK_AMBIGUOUS = "diff_hunk_ambiguous"
    RUN_LEASE_EXPIRED = "run_lease_expired"
    OWNER_FENCE_VIOLATION = "owner_fence_violation"


class PreparationConfig(_StrictFrozenModel):
    repository_key_env: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    version: str
    commit: str


class TerminalIncidentFacts(_StrictFrozenModel):
    graph_status: TerminalState
    closure_authoritative: bool
    internal_code: InternalIncidentCode | None = None
    excluded_disposition: Disposition | None = None
    rounded_duration_seconds: int = Field(ge=0)
    rounded_memory_mib: int = Field(ge=0)
    exception_class: PublicExceptionClass
    private_local_evidence: str = Field(max_length=100_000)

    @model_validator(mode="after")
    def exactly_one_disposition(self) -> TerminalIncidentFacts:
        if (self.internal_code is None) == (self.excluded_disposition is None):
            raise ValueError("exactly one incident code or excluded disposition is required")
        if self.excluded_disposition is Disposition.INTERNAL_PRODUCT_FAILURE:
            raise ValueError("internal product failure requires an internal incident code")
        return self


class PreparationReceipt(_StrictFrozenModel):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expiry: datetime


_INCIDENT_MAPPING = {
    InternalIncidentCode.DEADLINE_WATCHDOG_TIMEOUT: (
        Category.KERNEL,
        Failure.RUNTIME,
        Stage.RUNTIME,
        PublicExceptionClass.TIMEOUT_ERROR,
    ),
    InternalIncidentCode.STRUCTURED_OUTPUT_MISSING: (
        Category.WORKER,
        Failure.RUNTIME,
        Stage.WORKER,
        PublicExceptionClass.KEY_ERROR,
    ),
    InternalIncidentCode.ENVELOPE_INVALID: (
        Category.WORKER,
        Failure.VALUE,
        Stage.WORKER,
        PublicExceptionClass.VALUE_ERROR,
    ),
    InternalIncidentCode.WORKER_RESULT_ABSENT: (
        Category.WORKER,
        Failure.RUNTIME,
        Stage.WORKER,
        PublicExceptionClass.RUNTIME_ERROR,
    ),
    InternalIncidentCode.PROCESS_CLEANUP_FAILED: (
        Category.KERNEL,
        Failure.OS,
        Stage.RUNTIME,
        PublicExceptionClass.OS_ERROR,
    ),
    InternalIncidentCode.DIAGNOSTIC_PERSISTENCE_FAILED: (
        Category.STORAGE,
        Failure.OS,
        Stage.STORAGE,
        PublicExceptionClass.OS_ERROR,
    ),
    InternalIncidentCode.REPAIR_EXHAUSTED: (
        Category.KERNEL,
        Failure.RUNTIME,
        Stage.RUNTIME,
        PublicExceptionClass.RUNTIME_ERROR,
    ),
    InternalIncidentCode.DIFF_HUNK_AMBIGUOUS: (
        Category.KERNEL,
        Failure.VALUE,
        Stage.RUNTIME,
        PublicExceptionClass.VALUE_ERROR,
    ),
    InternalIncidentCode.RUN_LEASE_EXPIRED: (
        Category.KERNEL,
        Failure.RUNTIME,
        Stage.RUNTIME,
        PublicExceptionClass.TIMEOUT_ERROR,
    ),
    InternalIncidentCode.OWNER_FENCE_VIOLATION: (
        Category.KERNEL,
        Failure.RUNTIME,
        Stage.RUNTIME,
        PublicExceptionClass.RUNTIME_ERROR,
    ),
}


def _fail(code: str) -> Never:
    raise IncidentError(code)


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _validated(model_type: type[_ModelT], value: object, code: str) -> _ModelT:
    if not isinstance(value, model_type):
        _fail(code)
    try:
        return model_type.model_validate(value.model_dump(), strict=True)
    except Exception as error:
        raise IncidentError(code) from error


def _expiry(preview: object, policy: Policy, now: datetime) -> datetime:
    value = getattr(preview, "expiry", getattr(preview, "expires_at", None))
    if value is None:
        value = now + timedelta(hours=policy.approval_hours)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        _fail("INVALID_PREVIEW_EXPIRY")
    return value.astimezone(UTC)


def prepare_terminal_incident(
    config: PreparationConfig,
    facts: TerminalIncidentFacts,
    *,
    sanitizer: Callable[[str], str] | None,
    schema: type[Diagnosis] | None,
    outbox: Outbox | None,
    policy: Policy | None,
    now: datetime,
) -> PreparationReceipt | None:
    """Prepare and queue an incident; this function deliberately cannot publish."""

    config = _validated(PreparationConfig, config, "INVALID_PREPARATION_CONFIG")
    facts = _validated(TerminalIncidentFacts, facts, "INVALID_TERMINAL_FACTS")
    if sanitizer is None or not callable(sanitizer):
        _fail("MISSING_SANITIZER")
    if schema is not Diagnosis:
        _fail("MISSING_SCHEMA")
    if outbox is None or not isinstance(outbox, Outbox):
        _fail("MISSING_OUTBOX")
    policy = _validated(Policy, policy, "MISSING_POLICY")
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        _fail("INVALID_NOW")
    if policy.mode is Mode.OFF:
        return None
    if facts.excluded_disposition is not None:
        return None
    if facts.graph_status is not TerminalState.FAILED:
        return None
    if not facts.closure_authoritative:
        _fail("NON_AUTHORITATIVE_CLOSURE")
    if facts.internal_code is None:
        _fail("UNKNOWN_INTERNAL_INCIDENT_CODE")

    mapping = _INCIDENT_MAPPING.get(facts.internal_code)
    if mapping is None:
        _fail("UNKNOWN_INTERNAL_INCIDENT_CODE")
    category, failure, stage, exception_class = mapping
    if facts.exception_class is not exception_class:
        _fail("EXCEPTION_CLASS_MISMATCH")

    raw_key = os.environ.get(config.repository_key_env)
    if raw_key is None:
        _fail("MISSING_REPOSITORY_KEY")
    repository_key = raw_key.encode("utf-8")
    if len(repository_key) < 32:
        _fail("INVALID_REPOSITORY_KEY")

    private_detail = sanitizer(facts.private_local_evidence)
    if not isinstance(private_detail, str):
        _fail("SANITIZER_REJECTED")
    diagnosis = schema.model_validate(
        {
            "category": category,
            "terminal_state": TerminalState.FAILED,
            "disposition": Disposition.INTERNAL_PRODUCT_FAILURE,
            "failure": failure,
            "exception_class": exception_class,
            "stage": stage,
            "private_detail": private_detail,
        },
        strict=True,
    )
    report = compose(
        diagnosis,
        repository_key,
        config.version,
        config.commit,
        facts.rounded_duration_seconds,
        facts.rounded_memory_mib,
    )
    outbox.enqueue(report, policy, now)
    preview = outbox.preview(report.fingerprint, policy, repository_key, now)
    report_digest = hashlib.sha256(public_json(report).encode("utf-8")).hexdigest()
    return PreparationReceipt(
        fingerprint=report.fingerprint,
        report_digest=report_digest,
        preview_digest=preview.digest,
        expiry=_expiry(preview, policy, now),
    )
