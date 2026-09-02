"""Tests for terminal Graph-run incident integration."""

from __future__ import annotations

import json
import socket
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import ai_employee.incident_preparation as preparation
import ai_employee.incident_runtime as runtime
from ai_employee.domain import ExecutionPolicy, ExecutionStrategy, Goal, RoutingMode
from ai_employee.domain.harness import HarnessIncidentReporting, ProjectHarnessV2
from ai_employee.incident_preparation import InternalIncidentCode
from ai_employee.incident_reporting import Outbox
from ai_employee.incident_runtime import (
    INCIDENT_RUN_RECORD_KIND,
    IncidentRunErrorCode,
    IncidentRunRecord,
    IncidentRunState,
    prepare_graph_run_incidents,
)
from ai_employee.run_ownership import RunLeaseClosureRecord
from ai_employee.serialization import project_harness_digest
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import GraphRunRecord

NOW = datetime(2026, 9, 2, 3, 4, 5, tzinfo=UTC)
ZERO = "0" * 64
PUBLIC_COMMIT = "a" * 40
KEY = "repository-key-" + "k" * 32
CANARY = "private-runtime-canary"


def _strategy() -> ExecutionStrategy:
    return ExecutionStrategy(
        id="incident-fixture",
        routing_mode=RoutingMode.ADAPTIVE,
        backend="scripted",
        model="fixture",
    )


def _harness(
    tmp_path: Path,
    *,
    mode: str = "approval_required",
    auto_categories: tuple[str, ...] = (),
    auto_failures: tuple[str, ...] = (),
) -> ProjectHarnessV2:
    reporting = HarnessIncidentReporting(
        mode=mode,  # type: ignore[arg-type]
        target_repository="owner/repository",
        repository_key_env="ISSUE62_RUNTIME_KEY",
        auto_categories=auto_categories,  # type: ignore[arg-type]
        auto_failures=auto_failures,  # type: ignore[arg-type]
        outbox_path=str(tmp_path / "private" / "incident-outbox.sqlite3"),
        retention_hours=48,
        approval_hours=12,
        daily_limit=7,
        pending_cap=11,
    )
    return ProjectHarnessV2(incident_reporting=reporting)


def _run(
    harness: ProjectHarnessV2,
    run_id: str = "incident-run",
    *,
    status: str = "failed",
) -> GraphRunRecord:
    return GraphRunRecord(
        id=run_id,
        goal_id=f"goal-{run_id}",
        goal=Goal(id=f"goal-{run_id}", statement="exercise incident integration"),
        execution_policy=ExecutionPolicy(max_nodes=1, max_attempts=1),
        accepted_graph_revision_digest=ZERO,
        harness_digest=project_harness_digest(harness),
        effective_policy_digest="2" * 64,
        available_capabilities=(),
        execution_strategies=(_strategy(),),
        routing_mode=RoutingMode.ADAPTIVE,
        allowed_strategy_ids=("incident-fixture",),
        allowed_backends=("scripted",),
        local_backend_allowed=False,
        status=status,  # type: ignore[arg-type]
        max_concurrency=1,
        max_claims=1,
    )


def _closure(
    run: GraphRunRecord,
    record_id: str = "incident-closure",
    *,
    generation: int | None = None,
    terminal_status: str = "failed",
    reason: str = "failed",
) -> RunLeaseClosureRecord:
    return RunLeaseClosureRecord(
        id=record_id,
        run_id=run.id,
        created_at=NOW,
        graph_run_id=run.id,
        accepted_graph_revision_digest=run.accepted_graph_revision_digest,
        generation=run.generation if generation is None else generation,
        execution_attempt=run.execution_attempt,
        owner_instance_id="incident-owner",
        owner_record_id="incident-owner-record",
        owner_record_digest=ZERO,
        final_heartbeat_digest=ZERO,
        closed_at=NOW,
        terminal_graph_status=terminal_status,  # type: ignore[arg-type]
        reason=reason,
    )


def _put_closure(store: SQLiteStore, closure: RunLeaseClosureRecord) -> None:
    store.put("run_lease_closure_v2", closure, run_id=closure.run_id)


def _doctor(codes: list[object], run_id: str) -> dict[str, object]:
    return {
        "schema_version": "1",
        "run_id": run_id,
        "state": "failed",
        "authority": "read_only_classification",
        "incidents": [{"code": code, "private": CANARY} for code in codes],
    }


def _forbidden(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("forbidden boundary was accessed")


def test_off_and_nonfailed_return_before_inspector_outbox_or_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "inspect_graph_run", _forbidden)
    monkeypatch.setattr(runtime, "doctor_from_projection", _forbidden)
    monkeypatch.setattr(runtime, "Outbox", _forbidden)
    monkeypatch.setattr(preparation.os.environ, "get", _forbidden)

    with SQLiteStore(tmp_path / "local.db") as store:
        off_harness = ProjectHarnessV2()
        assert (
            prepare_graph_run_incidents(
                store,
                _run(off_harness),
                off_harness,
                public_commit=PUBLIC_COMMIT,
                clock=lambda: NOW,
            )
            == ()
        )
        active = _harness(tmp_path)
        assert (
            prepare_graph_run_incidents(
                store,
                _run(active, status="completed"),
                active,
                public_commit=PUBLIC_COMMIT,
                clock=lambda: NOW,
            )
            == ()
        )


@pytest.mark.parametrize("closure_case", ["missing", "mismatched", "ambiguous"])
def test_non_authoritative_closure_fails_closed_before_qualification(
    closure_case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    run = _run(harness)
    monkeypatch.setattr(runtime, "inspect_graph_run", _forbidden)
    monkeypatch.setattr(runtime, "Outbox", _forbidden)
    monkeypatch.setattr(preparation.os.environ, "get", _forbidden)

    with SQLiteStore(tmp_path / f"{closure_case}.db") as store:
        if closure_case == "mismatched":
            _put_closure(store, _closure(run, generation=run.generation + 1))
        elif closure_case == "ambiguous":
            _put_closure(store, _closure(run, "closure-one"))
            _put_closure(store, _closure(run, "closure-two"))

        records = prepare_graph_run_incidents(
            store,
            run,
            harness,
            public_commit=PUBLIC_COMMIT,
            clock=lambda: NOW,
        )
        assert len(records) == 1
        assert records[0].state is IncidentRunState.FAILED_CLOSED
        assert records[0].error_code is IncidentRunErrorCode.NON_AUTHORITATIVE_CLOSURE
        assert records[0].terminal_closure_digest is None


def test_closed_doctor_mapping_is_deduplicated_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    run = _run(harness)
    codes = list(runtime._DOCTOR_CODES)
    monkeypatch.setattr(runtime, "inspect_graph_run", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        runtime,
        "doctor_from_projection",
        lambda _projection: _doctor([*reversed(codes), codes[0]], run.id),
    )
    monkeypatch.setenv("ISSUE62_RUNTIME_KEY", KEY)

    with SQLiteStore(tmp_path / "mapping.db") as store:
        closure = _closure(run)
        _put_closure(store, closure)
        records = prepare_graph_run_incidents(
            store,
            run,
            harness,
            public_commit=PUBLIC_COMMIT,
            clock=lambda: NOW,
        )

        expected = sorted(runtime._DOCTOR_CODES.values(), key=lambda code: code.value)
        assert (
            sorted(
                (record.internal_incident_code for record in records),
                key=lambda code: "" if code is None else code.value,
            )
            == expected
        )
        assert len(records) == len(InternalIncidentCode)
        assert all(record.state is IncidentRunState.PREPARED for record in records)
        assert all(record.terminal_closure_digest == closure.content_digest for record in records)


def test_exact_current_closure_ignores_an_older_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    run = _run(harness)
    monkeypatch.setattr(runtime, "inspect_graph_run", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        runtime,
        "doctor_from_projection",
        lambda _projection: _doctor(["ENVELOPE_INVALID"], run.id),
    )
    monkeypatch.setenv("ISSUE62_RUNTIME_KEY", KEY)

    with SQLiteStore(tmp_path / "resumed.db") as store:
        _put_closure(store, _closure(run, "older", generation=run.generation + 1))
        current = _closure(run, "current")
        _put_closure(store, current)
        records = prepare_graph_run_incidents(
            store,
            run,
            harness,
            public_commit=PUBLIC_COMMIT,
            clock=lambda: NOW,
        )

    assert len(records) == 1
    assert records[0].state is IncidentRunState.PREPARED
    assert records[0].terminal_closure_digest == current.content_digest


def test_unknown_or_malformed_doctor_values_do_not_qualify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    run = _run(harness)
    monkeypatch.setattr(runtime, "inspect_graph_run", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        runtime,
        "doctor_from_projection",
        lambda _projection: _doctor(
            [CANARY, "WATCHDOG_TIMEOUT\0", object(), "POLICY_DENIED"], run.id
        ),
    )
    monkeypatch.setattr(runtime, "Outbox", _forbidden)
    monkeypatch.setattr(preparation.os.environ, "get", _forbidden)

    with SQLiteStore(tmp_path / "unknown.db") as store:
        _put_closure(store, _closure(run))
        assert (
            prepare_graph_run_incidents(
                store,
                run,
                harness,
                public_commit=PUBLIC_COMMIT,
                clock=lambda: NOW,
            )
            == ()
        )


def test_auto_failure_allowlist_exclusion_never_opens_outbox_or_reads_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(
        tmp_path,
        mode="auto",
        auto_categories=("worker_boundary_failure",),
        auto_failures=("os_error",),
    )
    run = _run(harness)
    monkeypatch.setattr(runtime, "inspect_graph_run", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        runtime,
        "doctor_from_projection",
        lambda _projection: _doctor(["ENVELOPE_INVALID"], run.id),
    )
    monkeypatch.setattr(runtime, "Outbox", _forbidden)
    monkeypatch.setattr(preparation.os.environ, "get", _forbidden)

    with SQLiteStore(tmp_path / "auto-excluded.db") as store:
        _put_closure(store, _closure(run))
        records = prepare_graph_run_incidents(
            store,
            run,
            harness,
            public_commit=PUBLIC_COMMIT,
            clock=lambda: NOW,
        )
        assert len(records) == 1
        assert records[0].state is IncidentRunState.EXCLUDED
        assert records[0].internal_incident_code is InternalIncidentCode.ENVELOPE_INVALID


def test_private_canaries_never_enter_receipt_or_outbox_and_no_publish_is_callable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    run = _run(harness)

    def projection(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "schema_version": "2",
            "run_id": run.id,
            "state": "failed",
            "worker_boundary_diagnostics": [
                {
                    "id": "diagnostic-one",
                    "content_digest": "3" * 64,
                    "code": "WORKER_ENVELOPE_MALFORMED",
                    "private_text": CANARY,
                }
            ],
        }

    monkeypatch.setattr(runtime, "inspect_graph_run", projection)
    monkeypatch.setattr(Outbox, "publish", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setenv("ISSUE62_RUNTIME_KEY", KEY)

    with SQLiteStore(tmp_path / "privacy.db") as store:
        _put_closure(store, _closure(run, reason=CANARY))
        records = prepare_graph_run_incidents(
            store,
            run,
            harness,
            public_commit=PUBLIC_COMMIT,
            clock=lambda: NOW,
        )
        assert len(records) == 1
        assert records[0].state is IncidentRunState.PREPARED

        local_dump = json.dumps([record.model_dump(mode="json") for record in records])
        outbox_path = Path(harness.incident_reporting.outbox_path).expanduser()
        connection = sqlite3.connect(outbox_path)
        try:
            outbox_dump = "\n".join(connection.iterdump())
        finally:
            connection.close()
        surface = local_dump + outbox_dump
        assert CANARY not in surface
        assert KEY not in surface
        assert harness.incident_reporting.repository_key_env not in surface
        assert "private_text" not in surface
        assert "report_body" not in local_dump


def test_preparation_exception_persists_one_bounded_failed_closed_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    run = _run(harness)
    monkeypatch.setattr(runtime, "inspect_graph_run", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        runtime,
        "doctor_from_projection",
        lambda _projection: _doctor(["WATCHDOG_TIMEOUT"], run.id),
    )

    def fail_outbox(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(runtime, "Outbox", fail_outbox)

    with SQLiteStore(tmp_path / "failed-closed.db") as store:
        _put_closure(store, _closure(run))
        records = prepare_graph_run_incidents(
            store,
            run,
            harness,
            public_commit=PUBLIC_COMMIT,
            clock=lambda: NOW,
        )
        assert len(records) == 1
        record = records[0]
        assert record.state is IncidentRunState.FAILED_CLOSED
        assert record.error_code is IncidentRunErrorCode.PREPARATION_FAILED
        assert CANARY not in record.model_dump_json()
        assert set(record.model_dump()) <= set(IncidentRunRecord.model_fields)


def test_persistence_is_run_scoped_frozen_and_idempotently_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    run = _run(harness, "bounded-run")
    monkeypatch.setattr(runtime, "inspect_graph_run", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        runtime,
        "doctor_from_projection",
        lambda _projection: _doctor(["ENVELOPE_INVALID"], run.id),
    )
    monkeypatch.setenv("ISSUE62_RUNTIME_KEY", KEY)

    with SQLiteStore(tmp_path / "bounded.db") as store:
        _put_closure(store, _closure(run))
        first = prepare_graph_run_incidents(
            store,
            run,
            harness,
            public_commit=PUBLIC_COMMIT,
            clock=lambda: NOW,
        )
        assert len(first) == 1

        monkeypatch.setattr(runtime, "inspect_graph_run", _forbidden)
        monkeypatch.delenv("ISSUE62_RUNTIME_KEY")
        second = prepare_graph_run_incidents(
            store,
            run,
            harness,
            public_commit=PUBLIC_COMMIT,
            clock=lambda: NOW,
        )
        assert second == first
        assert (
            store.list_records(
                INCIDENT_RUN_RECORD_KIND,
                IncidentRunRecord,
                run_id=run.id,
            )
            == first
        )
        assert (
            store.list_records(
                INCIDENT_RUN_RECORD_KIND,
                IncidentRunRecord,
                run_id="another-run",
            )
            == ()
        )

        with pytest.raises(ValidationError):
            first[0].state = IncidentRunState.PUBLISHED
        with pytest.raises(ValidationError):
            IncidentRunRecord.model_validate(first[0].model_dump() | {"private": CANARY})
