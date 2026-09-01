from __future__ import annotations

from datetime import UTC, datetime

from ai_employee.doctor import doctor_from_projection
from ai_employee.domain.v2 import WorkerBoundaryDiagnostic
from ai_employee.task_orchestration import WorkerTimeoutAuthorityRecord


def test_zero_effective_timeout_is_a_valid_exhausted_diagnostic() -> None:
    diagnostic = WorkerBoundaryDiagnostic(
        id="diagnostic-zero",
        run_id="child-run",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        adapter="fake",
        stage="runner",
        code="WORKER_BOUNDARY_ERROR",
        graph_run_id="parent-run",
        node_id="node-a",
        accepted_graph_revision_digest="0" * 64,
        generation=0,
        attempt=0,
        worker_request_id="request-a",
        worker_request_digest="1" * 64,
        duration_seconds=0.0,
        configured_timeout_seconds=2.0,
        effective_timeout_seconds=0.0,
    )

    assert diagnostic.effective_timeout_seconds == 0.0


def test_worker_timeout_authority_uses_the_strictest_accepted_ceiling() -> None:
    authority = WorkerTimeoutAuthorityRecord(
        id="timeout-authority",
        run_id="parent-run",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        graph_run_id="parent-run",
        node_id="node-a",
        child_run_id="child-run",
        accepted_graph_revision_digest="0" * 64,
        generation=0,
        attempt=0,
        adapter_timeout_seconds=1800.0,
        node_attempt_timeout_seconds=2.0,
        policy_timeout_seconds=1800.0,
        effective_timeout_seconds=2.0,
    )

    assert authority.effective_timeout_seconds == 2.0


def test_doctor_classifies_persisted_boundary_facts_without_mutation() -> None:
    projection = {
        "run_id": "parent-run",
        "state": "failed",
        "worker_timeout_authorities": [
            {
                "id": "timeout-a",
                "node_id": "node-a",
                "generation": 0,
                "attempt": 0,
                "effective_timeout_seconds": 3.0,
                "node_attempt_timeout_seconds": 2.0,
            }
        ],
        "node_watchdogs": [
            {
                "id": "watchdog-a",
                "node_id": "node-a",
                "generation": 0,
                "attempt": 0,
                "outcome": "cleanup_failed",
            }
        ],
        "node_control_propagations": [
            {
                "id": "control-a",
                "child_run_id": "child-a",
                "node_id": "node-a",
                "generation": 0,
                "attempt": 0,
                "propagated": False,
                "cleanup_confirmed": False,
            }
        ],
        "worker_boundary_diagnostics": [
            {
                "id": "diagnostic-a",
                "node_id": "node-a",
                "generation": 0,
                "attempt": 0,
                "code": "DIFF_HUNK_AMBIGUOUS",
                "process_status": "succeeded",
            },
            {
                "id": "diagnostic-b",
                "node_id": "node-a",
                "generation": 0,
                "attempt": 0,
                "code": "WORKER_STRUCTURED_OUTPUT_MISSING",
            },
            {
                "id": "diagnostic-c",
                "node_id": "node-a",
                "generation": 0,
                "attempt": 0,
                "code": "WORKER_ENVELOPE_MALFORMED",
            },
        ],
        "diagnostic_persistence_failures": [
            {"id": "fallback-a", "node_id": "node-a", "generation": 0, "attempt": 0}
        ],
        "nodes": [
            {
                "id": "execution-a",
                "node_id": "node-a",
                "generation": 0,
                "attempt": 0,
                "failure_code": "WORKER_BOUNDARY_ERROR",
                "worker_result_digest": None,
            }
        ],
        "loop_transitions": [
            {
                "id": "loop-a",
                "node_id": "node-a",
                "generation": 0,
                "attempt": 0,
                "action": "ESCALATE",
                "reason_code": "REPAIR_BUDGET_EXHAUSTED",
            }
        ],
    }

    report = doctor_from_projection(projection)

    assert report["authority"] == "read_only_classification"
    assert {item["code"] for item in report["incidents"]} == {
        "CANCEL_NOT_PROPAGATED",
        "DEADLINE_NOT_PROPAGATED",
        "DIAGNOSTIC_PERSISTENCE_FAILED",
        "DIFF_HUNK_AMBIGUOUS",
        "ENVELOPE_INVALID",
        "PROCESS_GROUP_CLEANUP_FAILED",
        "REPAIR_EXHAUSTED",
        "STRUCTURED_OUTPUT_MISSING",
        "WATCHDOG_TIMEOUT",
        "WORKER_RESULT_ABSENT",
    }
    assert projection["state"] == "failed"
