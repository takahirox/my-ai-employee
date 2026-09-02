"""Terminal Graph-run integration for the private incident outbox."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Literal, Self, cast

from pydantic import Field, TypeAdapter, model_validator

from . import __version__
from .doctor import doctor_from_projection
from .domain.base import Digest, Identifier, StableStrEnum, UtcTimestamp, ensure_utc
from .domain.harness import ProjectHarnessV2
from .domain.v2 import DigestedRecordV2
from .incident_preparation import (
    InternalIncidentCode,
    PreparationConfig,
    PreparationReceipt,
    TerminalIncidentFacts,
    prepare_terminal_incident,
)
from .incident_reporting import (
    Category,
    Diagnosis,
    Failure,
    IncidentError,
    Mode,
    Outbox,
    Policy,
    PublicationReceipt,
    PublicExceptionClass,
    TerminalState,
)
from .inspector import inspect_graph_run
from .run_ownership import RunLeaseClosureRecord
from .serialization import canonical_digest, project_harness_digest
from .storage import SQLiteStore
from .task_orchestration import GraphRunRecord

INCIDENT_RUN_RECORD_KIND = "incident_run_v2"


class IncidentRunState(StableStrEnum):
    """Closed lifecycle states for local incident evidence."""

    PREPARED = "prepared"
    EXCLUDED = "excluded"
    FAILED_CLOSED = "failed_closed"
    PUBLISHED = "published"


class IncidentRunErrorCode(StableStrEnum):
    """Bounded failure reasons safe to retain as local evidence."""

    NON_AUTHORITATIVE_CLOSURE = "non_authoritative_closure"
    HARNESS_BINDING_MISMATCH = "harness_binding_mismatch"
    INSPECTION_FAILED = "inspection_failed"
    CONFIGURATION_INVALID = "configuration_invalid"
    PREPARATION_FAILED = "preparation_failed"


class IncidentRunRecord(DigestedRecordV2):
    """Run-scoped receipt containing no report body or private diagnostic text."""

    schema_name: ClassVar[str] = "incident_run_record"
    graph_run_id: Identifier
    accepted_graph_revision_digest: Digest
    harness_digest: Digest
    effective_policy_digest: Digest
    generation: int = Field(ge=0)
    execution_attempt: int = Field(ge=0)
    terminal_closure_digest: Digest | None = None
    internal_incident_code: InternalIncidentCode | None = None
    state: IncidentRunState
    error_code: IncidentRunErrorCode | None = None
    fingerprint: Digest | None = None
    report_digest: Digest | None = None
    preview_digest: Digest | None = None
    expiry: UtcTimestamp | None = None
    issue_number: int | None = Field(default=None, ge=1)
    public_url: str | None = Field(
        default=None,
        max_length=300,
        pattern=(
            r"^https://github\.com/"
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
            r"[A-Za-z0-9._-]{1,100}/issues/[1-9][0-9]*$"
        ),
    )
    public_report_digest: Digest | None = None
    authorization_mode: Literal["approval_required", "auto"] | None = None
    authorization_digest: Digest | None = None
    authorized_at: UtcTimestamp | None = None
    published_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def _closed_shape(self) -> Self:
        if self.run_id != self.graph_run_id:
            raise ValueError("incident receipt must be stored under its graph run ID")

        receipt = (
            self.fingerprint,
            self.report_digest,
            self.preview_digest,
            self.expiry,
        )
        publication = (
            self.issue_number,
            self.public_url,
            self.public_report_digest,
            self.authorization_mode,
            self.authorization_digest,
            self.authorized_at,
            self.published_at,
        )
        has_receipt = all(value is not None for value in receipt)
        has_publication = all(value is not None for value in publication)
        if any(value is not None for value in receipt) and not has_receipt:
            raise ValueError("incident preparation receipt must be complete")
        if any(value is not None for value in publication) and not has_publication:
            raise ValueError("incident publication receipt must be complete")

        if self.state is IncidentRunState.FAILED_CLOSED:
            if self.error_code is None or has_receipt or has_publication:
                raise ValueError("failed-closed incident receipt has an invalid shape")
            return self

        if self.terminal_closure_digest is None or self.internal_incident_code is None:
            raise ValueError("qualified incident receipt requires terminal evidence")
        if self.error_code is not None:
            raise ValueError("nonfailure incident receipt cannot contain an error code")
        if self.state is IncidentRunState.EXCLUDED:
            if has_receipt or has_publication:
                raise ValueError("excluded incident receipt cannot contain public receipts")
        elif self.state is IncidentRunState.PREPARED:
            if not has_receipt or has_publication:
                raise ValueError("prepared incident receipt has an invalid shape")
        elif self.state is IncidentRunState.PUBLISHED:
            if not has_receipt or not has_publication:
                raise ValueError("published incident receipt has an invalid shape")
            issue_number = cast(int, self.issue_number)
            public_url = cast(str, self.public_url)
            public_report_digest = cast(str, self.public_report_digest)
            authorized_at = cast(datetime, self.authorized_at)
            published_at = cast(datetime, self.published_at)
            if public_report_digest != self.report_digest:
                raise ValueError("published report digest must match prepared report")
            if not public_url.endswith(f"/issues/{issue_number}"):
                raise ValueError("published incident URL must match its issue number")
            if authorized_at > published_at:
                raise ValueError("incident authorization must precede publication")
        return self


_DOCTOR_CODES: dict[str, InternalIncidentCode] = {
    "WATCHDOG_TIMEOUT": InternalIncidentCode.DEADLINE_WATCHDOG_TIMEOUT,
    "STRUCTURED_OUTPUT_MISSING": InternalIncidentCode.STRUCTURED_OUTPUT_MISSING,
    "ENVELOPE_INVALID": InternalIncidentCode.ENVELOPE_INVALID,
    "WORKER_RESULT_ABSENT": InternalIncidentCode.WORKER_RESULT_ABSENT,
    "PROCESS_GROUP_CLEANUP_FAILED": InternalIncidentCode.PROCESS_CLEANUP_FAILED,
    "DIAGNOSTIC_PERSISTENCE_FAILED": InternalIncidentCode.DIAGNOSTIC_PERSISTENCE_FAILED,
    "REPAIR_EXHAUSTED": InternalIncidentCode.REPAIR_EXHAUSTED,
    "DIFF_HUNK_AMBIGUOUS": InternalIncidentCode.DIFF_HUNK_AMBIGUOUS,
    "RUN_LEASE_EXPIRED": InternalIncidentCode.RUN_LEASE_EXPIRED,
    "OWNER_FENCE_VIOLATION": InternalIncidentCode.OWNER_FENCE_VIOLATION,
}

_CLASSIFICATION: dict[
    InternalIncidentCode,
    tuple[Category, Failure, PublicExceptionClass],
] = {
    InternalIncidentCode.DEADLINE_WATCHDOG_TIMEOUT: (
        Category.KERNEL,
        Failure.RUNTIME,
        PublicExceptionClass.TIMEOUT_ERROR,
    ),
    InternalIncidentCode.STRUCTURED_OUTPUT_MISSING: (
        Category.WORKER,
        Failure.RUNTIME,
        PublicExceptionClass.KEY_ERROR,
    ),
    InternalIncidentCode.ENVELOPE_INVALID: (
        Category.WORKER,
        Failure.VALUE,
        PublicExceptionClass.VALUE_ERROR,
    ),
    InternalIncidentCode.WORKER_RESULT_ABSENT: (
        Category.WORKER,
        Failure.RUNTIME,
        PublicExceptionClass.RUNTIME_ERROR,
    ),
    InternalIncidentCode.PROCESS_CLEANUP_FAILED: (
        Category.KERNEL,
        Failure.OS,
        PublicExceptionClass.OS_ERROR,
    ),
    InternalIncidentCode.DIAGNOSTIC_PERSISTENCE_FAILED: (
        Category.STORAGE,
        Failure.OS,
        PublicExceptionClass.OS_ERROR,
    ),
    InternalIncidentCode.REPAIR_EXHAUSTED: (
        Category.KERNEL,
        Failure.RUNTIME,
        PublicExceptionClass.RUNTIME_ERROR,
    ),
    InternalIncidentCode.DIFF_HUNK_AMBIGUOUS: (
        Category.KERNEL,
        Failure.VALUE,
        PublicExceptionClass.VALUE_ERROR,
    ),
    InternalIncidentCode.RUN_LEASE_EXPIRED: (
        Category.KERNEL,
        Failure.RUNTIME,
        PublicExceptionClass.TIMEOUT_ERROR,
    ),
    InternalIncidentCode.OWNER_FENCE_VIOLATION: (
        Category.KERNEL,
        Failure.RUNTIME,
        PublicExceptionClass.RUNTIME_ERROR,
    ),
}


def _record_id(
    graph_run: GraphRunRecord,
    closure_digest: str | None,
    state: IncidentRunState,
    code: InternalIncidentCode | None,
    error_code: IncidentRunErrorCode | None,
) -> str:
    digest = canonical_digest(
        {
            "graph_run_id": graph_run.id,
            "accepted_graph_revision_digest": graph_run.accepted_graph_revision_digest,
            "harness_digest": graph_run.harness_digest,
            "effective_policy_digest": graph_run.effective_policy_digest,
            "generation": graph_run.generation,
            "execution_attempt": graph_run.execution_attempt,
            "terminal_closure_digest": closure_digest,
            "state": state.value,
            "internal_incident_code": None if code is None else code.value,
            "error_code": None if error_code is None else error_code.value,
        }
    )
    return f"incident-{digest[:48]}"


def _record(
    graph_run: GraphRunRecord,
    now: datetime,
    *,
    closure_digest: str | None,
    state: IncidentRunState,
    code: InternalIncidentCode | None = None,
    error_code: IncidentRunErrorCode | None = None,
    receipt: PreparationReceipt | None = None,
) -> IncidentRunRecord:
    return IncidentRunRecord(
        id=_record_id(graph_run, closure_digest, state, code, error_code),
        run_id=graph_run.id,
        created_at=now,
        graph_run_id=graph_run.id,
        accepted_graph_revision_digest=graph_run.accepted_graph_revision_digest,
        harness_digest=graph_run.harness_digest,
        effective_policy_digest=graph_run.effective_policy_digest,
        generation=graph_run.generation,
        execution_attempt=graph_run.execution_attempt,
        terminal_closure_digest=closure_digest,
        internal_incident_code=code,
        state=state,
        error_code=error_code,
        fingerprint=None if receipt is None else receipt.fingerprint,
        report_digest=None if receipt is None else receipt.report_digest,
        preview_digest=None if receipt is None else receipt.preview_digest,
        expiry=None if receipt is None else receipt.expiry,
    )


def _persist(store: SQLiteStore, record: IncidentRunRecord) -> IncidentRunRecord:
    if store.put_once(INCIDENT_RUN_RECORD_KIND, record, run_id=record.run_id):
        return record
    for existing in store.list_records(
        INCIDENT_RUN_RECORD_KIND,
        IncidentRunRecord,
        run_id=record.run_id,
    ):
        if existing.id == record.id:
            return existing
    raise RuntimeError("incident receipt persistence failed")


def _persist_all(
    store: SQLiteStore,
    records: tuple[IncidentRunRecord, ...],
) -> tuple[IncidentRunRecord, ...]:
    return tuple(_persist(store, record) for record in records)


def _publication_record_id(prepared: IncidentRunRecord) -> str:
    digest = canonical_digest(
        {
            "prepared_record_id": prepared.id,
            "state": IncidentRunState.PUBLISHED.value,
        }
    )
    return f"incident-{digest[:48]}"


def _same_publication_record(
    left: IncidentRunRecord,
    right: IncidentRunRecord,
) -> bool:
    excluded = {"content_digest", "created_at"}
    return left.model_dump(mode="json", exclude=excluded) == right.model_dump(
        mode="json",
        exclude=excluded,
    )


def record_incident_publication(
    store: SQLiteStore,
    graph_run_id: Identifier,
    fingerprint: Digest,
    receipt: PublicationReceipt,
    *,
    clock: Callable[[], datetime],
) -> IncidentRunRecord:
    """Record a validated publication without publishing or changing the graph run."""

    try:
        if not isinstance(store, SQLiteStore):
            raise TypeError("store must be a SQLiteStore")
        if not callable(clock) or not isinstance(receipt, PublicationReceipt):
            raise TypeError("invalid publication dependencies")
        validated_graph_run_id = TypeAdapter(Identifier).validate_python(
            graph_run_id,
            strict=True,
        )
        validated_fingerprint = TypeAdapter(Digest).validate_python(
            fingerprint,
            strict=True,
        )
        validated_receipt = PublicationReceipt.model_validate(
            receipt.model_dump(mode="python", round_trip=True),
            strict=True,
        )
        now = ensure_utc(clock())
    except Exception:
        raise IncidentError("INVALID_PUBLICATION_ARGUMENTS") from None

    if validated_receipt.fingerprint != validated_fingerprint:
        raise IncidentError("PUBLICATION_FINGERPRINT_MISMATCH")
    if validated_receipt.published_at > now:
        raise IncidentError("PUBLICATION_TIME_INVALID")

    records = store.list_records(
        INCIDENT_RUN_RECORD_KIND,
        IncidentRunRecord,
        run_id=validated_graph_run_id,
    )
    matching = tuple(
        record
        for record in records
        if record.graph_run_id == validated_graph_run_id
        and record.fingerprint == validated_fingerprint
    )
    prepared = tuple(record for record in matching if record.state is IncidentRunState.PREPARED)
    if not prepared:
        raise IncidentError("PREPARED_INCIDENT_MISSING")
    if len(prepared) != 1:
        raise IncidentError("PREPARED_INCIDENT_AMBIGUOUS")
    prepared_record = prepared[0]
    if validated_receipt.public_report_digest != prepared_record.report_digest:
        raise IncidentError("PUBLICATION_REPORT_MISMATCH")

    try:
        candidate = IncidentRunRecord(
            id=_publication_record_id(prepared_record),
            run_id=prepared_record.run_id,
            created_at=now,
            graph_run_id=prepared_record.graph_run_id,
            accepted_graph_revision_digest=(prepared_record.accepted_graph_revision_digest),
            harness_digest=prepared_record.harness_digest,
            effective_policy_digest=prepared_record.effective_policy_digest,
            generation=prepared_record.generation,
            execution_attempt=prepared_record.execution_attempt,
            terminal_closure_digest=prepared_record.terminal_closure_digest,
            internal_incident_code=prepared_record.internal_incident_code,
            state=IncidentRunState.PUBLISHED,
            fingerprint=prepared_record.fingerprint,
            report_digest=prepared_record.report_digest,
            preview_digest=prepared_record.preview_digest,
            expiry=prepared_record.expiry,
            issue_number=validated_receipt.issue_number,
            public_url=validated_receipt.public_url,
            public_report_digest=validated_receipt.public_report_digest,
            authorization_mode=validated_receipt.authorization_mode,
            authorization_digest=validated_receipt.authorization_digest,
            authorized_at=validated_receipt.authorized_at,
            published_at=validated_receipt.published_at,
        )
    except Exception:
        raise IncidentError("PUBLICATION_RECEIPT_INVALID") from None

    published = tuple(record for record in matching if record.state is IncidentRunState.PUBLISHED)
    if len(published) > 1:
        raise IncidentError("PUBLISHED_INCIDENT_AMBIGUOUS")
    if published:
        if _same_publication_record(published[0], candidate):
            return published[0]
        raise IncidentError("PUBLISHED_INCIDENT_CONFLICT")

    if store.put_once(
        INCIDENT_RUN_RECORD_KIND,
        candidate,
        run_id=candidate.run_id,
    ):
        return candidate

    persisted = tuple(
        record
        for record in store.list_records(
            INCIDENT_RUN_RECORD_KIND,
            IncidentRunRecord,
            run_id=candidate.run_id,
        )
        if record.id == candidate.id
    )
    if len(persisted) == 1 and _same_publication_record(persisted[0], candidate):
        return persisted[0]
    raise IncidentError("PUBLICATION_PERSISTENCE_CONFLICT")


def _failed_closed(
    store: SQLiteStore,
    graph_run: GraphRunRecord,
    now: datetime,
    error_code: IncidentRunErrorCode,
    *,
    closure_digest: str | None = None,
    code: InternalIncidentCode | None = None,
) -> tuple[IncidentRunRecord, ...]:
    return (
        _persist(
            store,
            _record(
                graph_run,
                now,
                closure_digest=closure_digest,
                state=IncidentRunState.FAILED_CLOSED,
                code=code,
                error_code=error_code,
            ),
        ),
    )


def _exact_closure(
    store: SQLiteStore,
    graph_run: GraphRunRecord,
) -> RunLeaseClosureRecord | None:
    closures = store.list_records(
        "run_lease_closure_v2",
        RunLeaseClosureRecord,
        run_id=graph_run.id,
    )
    matching = tuple(
        closure
        for closure in closures
        if closure.run_id == graph_run.id
        and closure.graph_run_id == graph_run.id
        and closure.accepted_graph_revision_digest == graph_run.accepted_graph_revision_digest
        and closure.generation == graph_run.generation
        and closure.execution_attempt == graph_run.execution_attempt
        and closure.terminal_graph_status == "failed"
    )
    if len(matching) != 1:
        return None
    return matching[0]


def _existing_receipts(
    store: SQLiteStore,
    graph_run: GraphRunRecord,
    closure_digest: str,
) -> tuple[IncidentRunRecord, ...]:
    records = store.list_records(
        INCIDENT_RUN_RECORD_KIND,
        IncidentRunRecord,
        run_id=graph_run.id,
    )
    matching = (
        record
        for record in records
        if record.graph_run_id == graph_run.id
        and record.accepted_graph_revision_digest == graph_run.accepted_graph_revision_digest
        and record.harness_digest == graph_run.harness_digest
        and record.effective_policy_digest == graph_run.effective_policy_digest
        and record.generation == graph_run.generation
        and record.execution_attempt == graph_run.execution_attempt
        and record.terminal_closure_digest == closure_digest
    )
    return tuple(sorted(matching, key=lambda record: record.id))


def _incident_codes(doctor: object, run_id: str) -> tuple[InternalIncidentCode, ...]:
    if not isinstance(doctor, Mapping):
        return ()
    if (
        doctor.get("schema_version") != "1"
        or doctor.get("run_id") != run_id
        or doctor.get("state") != "failed"
        or doctor.get("authority") != "read_only_classification"
    ):
        return ()
    incidents = doctor.get("incidents")
    if not isinstance(incidents, list):
        return ()
    codes: set[InternalIncidentCode] = set()
    for incident in incidents:
        if not isinstance(incident, Mapping):
            continue
        raw_code = incident.get("code")
        if isinstance(raw_code, str) and raw_code in _DOCTOR_CODES:
            codes.add(_DOCTOR_CODES[raw_code])
    return tuple(sorted(codes, key=lambda code: code.value))


def incident_policy_from_harness(
    harness: ProjectHarnessV2,
) -> tuple[Policy, frozenset[Failure]]:
    """Derive the closed public policy and failure allowlist from a Harness."""
    reporting = harness.incident_reporting
    if reporting.target_repository is None or reporting.repository_key_env is None:
        raise ValueError("enabled incident reporting is incomplete")
    policy = Policy(
        mode=Mode(reporting.mode),
        repository=reporting.target_repository,
        auto_categories=tuple(Category(value) for value in reporting.auto_categories),
        retention_hours=reporting.retention_hours,
        approval_hours=reporting.approval_hours,
        daily_limit=reporting.daily_limit,
        pending_cap=reporting.pending_cap,
    )
    # Policy owns category authorization; its current public contract has no failure
    # field, so the exact Harness failure allowlist remains enforced at this boundary.
    return policy, frozenset(Failure(value) for value in reporting.auto_failures)


def prepare_graph_run_incidents(
    store: SQLiteStore,
    graph_run: GraphRunRecord,
    harness: ProjectHarnessV2,
    *,
    public_commit: str,
    clock: Callable[[], datetime],
) -> tuple[IncidentRunRecord, ...]:
    """Prepare allowlisted terminal incidents without publishing or changing the run."""

    reporting = harness.incident_reporting
    if reporting.mode == "off":
        return ()
    if graph_run.status != "failed":
        return ()

    now = ensure_utc(clock())
    closure = _exact_closure(store, graph_run)
    if closure is None or closure.content_digest is None:
        return _failed_closed(
            store,
            graph_run,
            now,
            IncidentRunErrorCode.NON_AUTHORITATIVE_CLOSURE,
        )
    closure_digest = closure.content_digest

    try:
        harness_matches = project_harness_digest(harness) == graph_run.harness_digest
    except Exception:
        harness_matches = False
    if not harness_matches:
        return _failed_closed(
            store,
            graph_run,
            now,
            IncidentRunErrorCode.HARNESS_BINDING_MISMATCH,
            closure_digest=closure_digest,
        )

    existing = _existing_receipts(store, graph_run, closure_digest)
    if existing:
        return existing

    try:
        projection = inspect_graph_run(store, graph_run.id, clock=lambda: now)
        doctor = doctor_from_projection(projection)
        codes = _incident_codes(doctor, graph_run.id)
    except Exception:
        return _failed_closed(
            store,
            graph_run,
            now,
            IncidentRunErrorCode.INSPECTION_FAILED,
            closure_digest=closure_digest,
        )
    if not codes:
        return ()

    try:
        policy, auto_failures = incident_policy_from_harness(harness)
    except Exception:
        return _failed_closed(
            store,
            graph_run,
            now,
            IncidentRunErrorCode.CONFIGURATION_INVALID,
            closure_digest=closure_digest,
        )

    included: list[InternalIncidentCode] = []
    excluded: list[InternalIncidentCode] = []
    for code in codes:
        category, failure, _exception_class = _CLASSIFICATION[code]
        if policy.mode is Mode.AUTO and (
            category not in policy.auto_categories or failure not in auto_failures
        ):
            excluded.append(code)
        else:
            included.append(code)

    excluded_records = tuple(
        _record(
            graph_run,
            now,
            closure_digest=closure_digest,
            state=IncidentRunState.EXCLUDED,
            code=code,
        )
        for code in excluded
    )
    if not included:
        return _persist_all(store, excluded_records)

    try:
        repository_key_env = reporting.repository_key_env
        if repository_key_env is None:
            raise ValueError("enabled incident reporting has no repository key binding")
        config = PreparationConfig(
            repository_key_env=repository_key_env,
            version=__version__,
            commit=public_commit,
        )
        prepared: list[IncidentRunRecord] = []
        with Outbox(Path(reporting.outbox_path).expanduser()) as outbox:
            for code in included:
                _category, _failure, exception_class = _CLASSIFICATION[code]
                receipt = prepare_terminal_incident(
                    config,
                    TerminalIncidentFacts(
                        graph_status=TerminalState.FAILED,
                        closure_authoritative=True,
                        internal_code=code,
                        rounded_duration_seconds=0,
                        rounded_memory_mib=0,
                        exception_class=exception_class,
                        private_local_evidence=code.value,
                    ),
                    sanitizer=lambda value: value,
                    schema=Diagnosis,
                    outbox=outbox,
                    policy=policy,
                    now=now,
                )
                if receipt is None:
                    raise ValueError("qualified incident was not prepared")
                prepared.append(
                    _record(
                        graph_run,
                        now,
                        closure_digest=closure_digest,
                        state=IncidentRunState.PREPARED,
                        code=code,
                        receipt=receipt,
                    )
                )
    except Exception:
        return _failed_closed(
            store,
            graph_run,
            now,
            IncidentRunErrorCode.PREPARATION_FAILED,
            closure_digest=closure_digest,
        )

    records = (*excluded_records, *prepared)
    return _persist_all(store, tuple(sorted(records, key=lambda record: record.id)))
