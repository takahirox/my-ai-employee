from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ai_employee.domain import GoalTaskKind
from ai_employee.domain.v2 import DecisionOutcome, ExecutionResult, PolicyDecision, WorkerRequest
from ai_employee.orchestration import WorkCoordinator
from ai_employee.storage import SQLiteStore
from ai_employee.worker_adapters import CodexCliWorkerAdapter
from ai_employee.worker_attribution import attribute_read_only_payload, model_read_only_schema
from tests.test_graph_typed_results import _execute
from tests.test_work_orchestration_v2 import NOW, ZERO, Channel, worker_request


def request(attempt: int = 0) -> WorkerRequest:
    return WorkerRequest.model_validate(
        {
            **worker_request().model_dump(exclude={"content_digest"}),
            "task_kind": GoalTaskKind.NON_MUTATING,
            "processes_authorized": False,
            "graph_run_id": "graph",
            "node_id": "node",
            "accepted_graph_revision_digest": ZERO,
            "attempt": attempt,
        },
        strict=True,
    )


def payload(**changes: object) -> str:
    return json.dumps(
        {
            "schema_version": "2",
            "proposals": [],
            "assistant_note": "",
            "usage_json": "{}",
            "non_mutating_result": {
                "schema_version": "3",
                "logical_kind": "diagnosis",
                "media_type": "text/plain",
                "content": "Observed a stale cache key.",
                "summary": None,
                "findings": [],
                "evidence_refs": [],
                **changes,
            },
        }
    )


def test_model_schema_contains_only_substantive_output() -> None:
    schema = model_read_only_schema()
    assert set(schema["properties"]) == {
        "schema_version",
        "logical_kind",
        "media_type",
        "content",
        "summary",
        "findings",
        "evidence_refs",
    }
    assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.parametrize("attempt", [0, 1, 4])
def test_runtime_assigns_originating_attempt_not_current_global_state(attempt: int) -> None:
    req = request(attempt)
    result = json.loads(attribute_read_only_payload(payload(), req))["non_mutating_result"]
    assert result["schema_version"] == "2"  # stable authoritative storage contract
    assert result["attempt"] == attempt and result["worker_request_digest"] == req.content_digest
    assert result["run_id"] == req.run_id and result["node_id"] == "node"


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "foreign"),
        ("attempt", 7),
        ("worker_request_digest", ZERO),
        ("content", " "),
        ("evidence_refs", ["src/file.py:5"]),
    ],
)
def test_extra_attribution_or_malformed_content_is_rejected(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        attribute_read_only_payload(payload(**{field: value}), request())


def test_legacy_output_is_not_relabelled() -> None:
    raw = json.loads(payload())
    raw["non_mutating_result"].update(schema_version="2", run_id="foreign", attempt=91)
    encoded = json.dumps(raw)
    assert attribute_read_only_payload(encoded, request()) == encoded


@pytest.mark.parametrize("foreign,cancelled", [(False, False), (True, False), (False, True)])
def test_native_adapter_checks_process_correlation_and_cancellation(
    foreign: bool,
    cancelled: bool,
) -> None:
    read = []

    class Executor:
        def execute(self, req, decision, cancellation):
            return ExecutionResult(
                id="process-result",
                run_id="foreign" if foreign else req.run_id,
                request_digest=req.content_digest,
                created_at=NOW,
                status="succeeded",
                duration_seconds=0.01,
                stdout_artifact_digest=ZERO,
            )

    class Cancellation:
        def cancelled(self):
            return cancelled

    adapter = CodexCliWorkerAdapter(
        Executor(),
        lambda _: (read.append(True), payload().encode())[1],
        lambda req: PolicyDecision(
            id="decision",
            run_id=req.run_id,
            created_at=NOW,
            request_digest=req.content_digest,
            effective_policy_digest=ZERO,
            outcome=DecisionOutcome.ALLOW,
            reason_code="test_allowed",
            limits={},
        ),
        run_id="run-1",
        cancellation=Cancellation(),
    )
    result = adapter.propose(request(), Channel())
    assert (result.status == "succeeded") == (not foreign and not cancelled)
    assert bool(read) == (not foreign and not cancelled)
    if cancelled and not foreign:
        assert result.failure.code.value == "CANCELLED" and result.status == "cancelled"
    if foreign:
        assert result.boundary_diagnostic.code == "WORKER_PROCESS_BINDING_INVALID"
    if result.non_mutating_result:
        assert result.non_mutating_result.worker_request_digest == request().content_digest


def test_atomic_acceptance_rolls_back_on_event_write_failure(tmp_path: Path, monkeypatch) -> None:
    original = WorkCoordinator._accept_non_mutating_result
    observed = []

    def fail_at_commit(self, req, result, count):
        self.store._connection.execute("""CREATE TEMP TRIGGER fail_typed_event
            BEFORE INSERT ON work_events_v2 WHEN NEW.payload LIKE '%typed_result_accepted%'
            BEGIN SELECT RAISE(ABORT, 'simulated crash'); END""")
        try:
            with pytest.raises(sqlite3.IntegrityError, match="simulated crash"):
                original(self, req, result, count)
            observed.append(
                self.store._connection.execute(
                    "SELECT count(*) FROM records WHERE kind IN "
                    "('non_mutating_result_acceptance_v2','artifact_descriptor_v2') AND run_id=?",
                    (req.run_id,),
                ).fetchone()[0]
            )
        finally:
            self.store._connection.execute("DROP TRIGGER fail_typed_event")
        return original(self, req, result, count)

    monkeypatch.setattr(WorkCoordinator, "_accept_non_mutating_result", fail_at_commit)
    _execute(tmp_path)
    assert observed == [0, 0]


@pytest.mark.parametrize("race", ["cancel", "supersede"])
def test_atomic_acceptance_refuses_cancelled_or_superseded_invocation(tmp_path, monkeypatch, race):
    original = SQLiteStore.commit_typed_result_acceptance
    observed = []

    def race_before_commit(self, acceptance, event):
        active = self.get_work_run(acceptance.run_id)
        if race == "cancel":
            self.request_control(active.id, "cancel")
        else:
            self.save_work_run(active.model_copy(update={"worker_request_digest": "f" * 64}))
        with pytest.raises(ValueError, match="active invocation"):
            original(self, acceptance, event)
        observed.append(
            self._connection.execute(
                "SELECT count(*) FROM records WHERE kind IN "
                "('non_mutating_result_acceptance_v2','artifact_descriptor_v2') AND run_id=?",
                (active.id,),
            ).fetchone()[0]
        )
        self.clear_control(active.id)
        self.save_work_run(active)
        return original(self, acceptance, event)

    monkeypatch.setattr(SQLiteStore, "commit_typed_result_acceptance", race_before_commit)
    _execute(tmp_path)
    assert observed == [0, 0]
