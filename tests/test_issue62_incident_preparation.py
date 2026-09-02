"""Integration tests for terminal incident preparation."""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import ai_employee.incident_preparation as preparation
from ai_employee.incident_preparation import (
    InternalIncidentCode,
    PreparationConfig,
    PreparationReceipt,
    TerminalIncidentFacts,
    prepare_terminal_incident,
)
from ai_employee.incident_reporting import (
    Diagnosis,
    Disposition,
    IncidentError,
    Mode,
    Outbox,
    Policy,
    PublicExceptionClass,
    TerminalState,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
KEY = "repository-key-" + "k" * 32
CANARY = "private-incident-canary"
CONFIG = PreparationConfig(
    repository_key_env="ISSUE62_REPOSITORY_KEY",
    version="1.2.3",
    commit="a" * 40,
)

EXPECTED_EXCEPTIONS = {
    InternalIncidentCode.DEADLINE_WATCHDOG_TIMEOUT: PublicExceptionClass.TIMEOUT_ERROR,
    InternalIncidentCode.STRUCTURED_OUTPUT_MISSING: PublicExceptionClass.KEY_ERROR,
    InternalIncidentCode.ENVELOPE_INVALID: PublicExceptionClass.VALUE_ERROR,
    InternalIncidentCode.WORKER_RESULT_ABSENT: PublicExceptionClass.RUNTIME_ERROR,
    InternalIncidentCode.PROCESS_CLEANUP_FAILED: PublicExceptionClass.OS_ERROR,
    InternalIncidentCode.DIAGNOSTIC_PERSISTENCE_FAILED: PublicExceptionClass.OS_ERROR,
    InternalIncidentCode.REPAIR_EXHAUSTED: PublicExceptionClass.RUNTIME_ERROR,
    InternalIncidentCode.DIFF_HUNK_AMBIGUOUS: PublicExceptionClass.VALUE_ERROR,
    InternalIncidentCode.RUN_LEASE_EXPIRED: PublicExceptionClass.TIMEOUT_ERROR,
    InternalIncidentCode.OWNER_FENCE_VIOLATION: PublicExceptionClass.RUNTIME_ERROR,
}


def facts(
    code: InternalIncidentCode = InternalIncidentCode.ENVELOPE_INVALID,
    **changes: object,
) -> TerminalIncidentFacts:
    values: dict[str, object] = {
        "graph_status": TerminalState.FAILED,
        "closure_authoritative": True,
        "internal_code": code,
        "rounded_duration_seconds": 5,
        "rounded_memory_mib": 64,
        "exception_class": EXPECTED_EXCEPTIONS[code],
        "private_local_evidence": CANARY,
    }
    values.update(changes)
    return TerminalIncidentFacts.model_validate(values)


def policy(mode: Mode = Mode.APPROVAL_REQUIRED) -> Policy:
    return Policy(mode=mode, repository="owner/repository")


def invoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate: TerminalIncidentFacts,
) -> tuple[Outbox, PreparationReceipt | None]:
    monkeypatch.setenv(CONFIG.repository_key_env, KEY)
    box = Outbox(tmp_path / "private" / "outbox.sqlite3")
    receipt = prepare_terminal_incident(
        CONFIG,
        candidate,
        sanitizer=lambda value: value,
        schema=Diagnosis,
        outbox=box,
        policy=policy(),
        now=NOW,
    )
    return box, receipt


@pytest.mark.parametrize("code", tuple(InternalIncidentCode))
def test_every_allowlisted_code_has_a_fixed_mapping(
    code: InternalIncidentCode,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = []
    real_compose = preparation.compose

    def capture(diagnosis, *args):
        captured.append(diagnosis)
        return real_compose(diagnosis, *args)

    monkeypatch.setattr(preparation, "compose", capture)
    box, receipt = invoke(tmp_path, monkeypatch, facts(code))
    try:
        expected = preparation._INCIDENT_MAPPING[code]
        assert (
            captured[0].category,
            captured[0].failure,
            captured[0].stage,
            captured[0].exception_class,
        ) == expected
        assert isinstance(receipt, PreparationReceipt)
    finally:
        box.close()


@pytest.mark.parametrize(
    "disposition",
    tuple(value for value in Disposition if value is not Disposition.INTERNAL_PRODUCT_FAILURE),
)
def test_every_expected_exclusion_is_a_noop(
    disposition: Disposition,
    tmp_path: Path,
) -> None:
    with Outbox(tmp_path / "outbox.sqlite3") as box:
        candidate = facts().model_copy(
            update={"internal_code": None, "excluded_disposition": disposition}
        )
        assert (
            prepare_terminal_incident(
                CONFIG,
                candidate,
                sanitizer=lambda value: value,
                schema=Diagnosis,
                outbox=box,
                policy=policy(),
                now=NOW,
            )
            is None
        )


def test_off_and_nonfailed_are_noops_without_reading_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CONFIG.repository_key_env, raising=False)
    with Outbox(tmp_path / "outbox.sqlite3") as box:
        arguments = {
            "sanitizer": lambda value: value,
            "schema": Diagnosis,
            "outbox": box,
            "now": NOW,
        }
        assert (
            prepare_terminal_incident(CONFIG, facts(), policy=policy(Mode.OFF), **arguments) is None
        )
        assert (
            prepare_terminal_incident(
                CONFIG,
                facts().model_copy(update={"graph_status": TerminalState.CANCELLED}),
                policy=policy(),
                **arguments,
            )
            is None
        )


def test_unbound_unknown_and_non_authoritative_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError):
        TerminalIncidentFacts.model_validate(
            facts().model_dump() | {"internal_code": None, "excluded_disposition": None},
            strict=True,
        )
    malformed = facts().model_copy(update={"internal_code": "future_failure"})
    monkeypatch.setenv(CONFIG.repository_key_env, KEY)
    with Outbox(tmp_path / "outbox.sqlite3") as box:
        arguments = {
            "sanitizer": lambda value: value,
            "schema": Diagnosis,
            "outbox": box,
            "policy": policy(),
            "now": NOW,
        }
        with pytest.raises(IncidentError):
            prepare_terminal_incident(CONFIG, malformed, **arguments)
        with pytest.raises(IncidentError, match="NON_AUTHORITATIVE_CLOSURE"):
            prepare_terminal_incident(
                CONFIG,
                facts().model_copy(update={"closure_authoritative": False}),
                **arguments,
            )


@pytest.mark.parametrize("key", [None, "short"])
def test_missing_and_short_repository_keys_fail(
    key: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if key is None:
        monkeypatch.delenv(CONFIG.repository_key_env, raising=False)
    else:
        monkeypatch.setenv(CONFIG.repository_key_env, key)
    with (
        Outbox(tmp_path / "outbox.sqlite3") as box,
        pytest.raises(IncidentError, match="REPOSITORY_KEY"),
    ):
        prepare_terminal_incident(
            CONFIG,
            facts(),
            sanitizer=lambda value: value,
            schema=Diagnosis,
            outbox=box,
            policy=policy(),
            now=NOW,
        )


def test_receipt_is_deterministic_and_private_values_never_persist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_box, first = invoke(tmp_path / "one", monkeypatch, facts())
    second_box, second = invoke(tmp_path / "two", monkeypatch, facts())
    try:
        assert first == second
        assert first is not None
        surface = json.dumps(first.model_dump(mode="json")) + "\n".join(first_box.db.iterdump())
        assert CANARY not in surface
        assert KEY not in surface
        assert set(first.model_dump()) == {
            "fingerprint",
            "report_digest",
            "preview_digest",
            "expiry",
        }
    finally:
        first_box.close()
        second_box.close()


@pytest.mark.parametrize(
    ("argument", "value"),
    (("sanitizer", None), ("schema", None), ("outbox", None), ("policy", None)),
)
def test_required_boundary_dependencies_cannot_be_missing(
    argument: str,
    value: object,
    tmp_path: Path,
) -> None:
    with Outbox(tmp_path / "outbox.sqlite3") as box:
        arguments = {
            "sanitizer": lambda candidate: candidate,
            "schema": Diagnosis,
            "outbox": box,
            "policy": policy(),
            "now": NOW,
        }
        arguments[argument] = value
        with pytest.raises(IncidentError):
            prepare_terminal_incident(CONFIG, facts(), **arguments)


def test_models_are_strict_frozen_and_forbid_extra() -> None:
    for instance in (CONFIG, facts()):
        with pytest.raises(ValidationError):
            type(instance).model_validate(instance.model_dump() | {"extra": True})
        field = next(iter(type(instance).model_fields))
        with pytest.raises(ValidationError):
            setattr(instance, field, getattr(instance, field))

    with pytest.raises(ValidationError):
        PreparationConfig.model_validate(
            {
                "repository_key_env": 7,
                "version": "1.2.3",
                "commit": "a" * 40,
            }
        )


def test_preparation_never_publishes_or_opens_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("publication or network access attempted")

    monkeypatch.setattr(Outbox, "publish", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    box, receipt = invoke(tmp_path, monkeypatch, facts())
    try:
        assert receipt is not None
    finally:
        box.close()
