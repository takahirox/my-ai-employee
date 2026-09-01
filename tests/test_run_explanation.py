from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_employee.cli import build_parser, main
from ai_employee.domain.v2 import (
    DecisionOutcome,
    ExecutionResult,
    PolicyDecision,
    StableFailure,
    StableFailureCode,
)
from ai_employee.inspector import inspect_any_run, inspect_fleet_runs
from ai_employee.orchestration import WorkRun
from ai_employee.run_explanation import _explain_graph_run, explain_any_run
from ai_employee.serialization import canonical_json
from ai_employee.storage import SQLiteStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = "0" * 64


def test_explain_cli_is_a_distinct_read_only_command() -> None:
    args = build_parser().parse_args(("explain", "run-1", "--db", "fleet.db"))

    assert args.command == "explain"
    assert args.run_id == "run-1"
    assert args.db == "fleet.db"


def test_work_explanation_includes_policy_denial_as_a_failure_cause(tmp_path: Path) -> None:
    run = WorkRun(
        id="denied-work",
        goal="attempt a bounded process",
        repository=str(tmp_path),
        base_commit="base",
        worker="scripted",
        status="failed",
        effective_policy_digest=ZERO,
        node_id="node-denied",
        failure_code="POLICY_DENIED",
        worker_request_digest="9" * 64,
    )
    decision = PolicyDecision(
        id="decision-denied",
        run_id=run.id,
        created_at=NOW,
        request_digest="1" * 64,
        effective_policy_digest=ZERO,
        outcome=DecisionOutcome.DENY,
        reason_code="operator_policy_denied",
    )
    with SQLiteStore(tmp_path / "fleet.db") as store:
        store.save_work_run(run)
        store.put("policy_decision_v2", decision, run_id=run.id)

        explanation = explain_any_run(store, run.id)

        assert explanation["task_stories"][0]["policy_decisions"] == [
            {
                "outcome": "deny",
                "reason_code": "operator_policy_denied",
                "request_digest": "1" * 64,
            }
        ]
        assert explanation["failure_path"] == [
            {
                "stage": "policy",
                "outcome": "deny",
                "reason_code": "operator_policy_denied",
                "request_digest": "1" * 64,
            },
            {"stage": "run", "reason_code": "POLICY_DENIED"},
        ]


def test_unknown_run_is_not_reconstructed_or_created(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "fleet.db") as store:
        with pytest.raises(KeyError):
            explain_any_run(store, "missing")

        assert store.list_records("graph_run_v2", WorkRun) == ()


def test_historical_standalone_work_run_is_hidden_without_deleting_its_row(
    tmp_path: Path,
) -> None:
    run = WorkRun(
        id="historical-work",
        goal="legacy standalone work",
        repository=str(tmp_path),
        base_commit="base",
        worker="scripted",
        status="failed",
        effective_policy_digest=ZERO,
    )
    with SQLiteStore(tmp_path / "historical.db") as store:
        store.save_work_run(run)

        assert store.list_run_repositories() == ()
        assert inspect_fleet_runs(store) == {"active": [], "history": []}
        for reader in (inspect_any_run, explain_any_run):
            with pytest.raises(KeyError, match=run.id):
                reader(store, run.id)
        assert store.get_work_run(run.id) == run


@pytest.mark.parametrize(
    "command",
    (
        ("inspect", "historical-work"),
        ("explain", "historical-work"),
        ("resume", "historical-work"),
        ("diff", "historical-work"),
        ("logs", "historical-work"),
        ("promote", "historical-work", "--patch-digest", ZERO),
        ("pause", "historical-work"),
        ("cancel", "historical-work"),
    ),
)
def test_historical_standalone_work_run_is_hidden_from_operational_commands(
    tmp_path: Path, command: tuple[str, ...]
) -> None:
    database = tmp_path / "historical.db"
    run = WorkRun(
        id="historical-work",
        goal="legacy standalone work",
        repository=str(tmp_path),
        base_commit="base",
        worker="scripted",
        status="failed",
        effective_policy_digest=ZERO,
    )
    with SQLiteStore(database) as store:
        store.save_work_run(run)

    with pytest.raises(KeyError, match=run.id):
        main((*command, "--db", str(database)))

    with SQLiteStore(database) as store:
        assert store.get_work_run(run.id) == run
        assert store.control(run.id) is None


def test_work_verification_explanation_does_not_expose_failure_text(tmp_path: Path) -> None:
    canary = "TOP-SECRET-FAILURE-CANARY"
    run = WorkRun(
        id="redacted-verification",
        goal="verify without exposing process output",
        repository=str(tmp_path),
        base_commit="base",
        worker="scripted",
        status="failed",
        effective_policy_digest=ZERO,
        node_id="node-verification",
        failure_code="VERIFICATION_FAILED",
        worker_request_digest="8" * 64,
    )
    result = ExecutionResult(
        id="verification-failed",
        run_id=run.id,
        created_at=NOW,
        request_digest="2" * 64,
        status="failed",
        failure=StableFailure(
            code=StableFailureCode.VERIFICATION_FAILED,
            message=canary,
            retryable=True,
            details={"captured_output": canary},
        ),
        duration_seconds=0.1,
    )
    with SQLiteStore(tmp_path / "redacted.db") as store:
        store.save_work_run(run)
        store.put("verification_result_v2", result, run_id=run.id)

        explanation = explain_any_run(store, run.id)

    assert explanation["task_stories"][0]["verification"] == [
        {
            "status": "failed",
            "failure": {"code": "VERIFICATION_FAILED", "retryable": True},
        }
    ]
    assert canary not in canonical_json(explanation)
    assert "captured_output" not in canonical_json(explanation)


@pytest.mark.parametrize("decision", ("denied", "expired"))
def test_graph_explanation_surfaces_terminal_promotion_approval(decision: str) -> None:
    request_digest = "3" * 64
    policy_digest = "4" * 64
    view = {
        "run_id": "promotion-run",
        "kind": "graph_run",
        "state": "ready_to_promote",
        "generation": 0,
        "run": {
            "goal_id": "goal",
            "goal": {"id": "goal", "statement": "promote an accepted patch"},
            "effective_policy_digest": policy_digest,
            "promotion_approval_id": "approval",
            "promotion_approval_request_digest": request_digest,
        },
        "approval_requests": [
            {
                "id": "approval-request",
                "request_digest": request_digest,
                "policy_digest": policy_digest,
                "approval_classes": ["promotion"],
            }
        ],
        "approvals": [
            {
                "id": "approval",
                "request_digest": request_digest,
                "policy_digest": policy_digest,
                "scope": [request_digest],
                "decision": decision,
            }
        ],
    }

    explanation = _explain_graph_run(view)

    assert explanation["current_state"]["promotion_approval_state"] == decision
    assert explanation["failure_path"][-1] == {
        "stage": "promotion_approval",
        "outcome": decision,
        "approval_id": "approval",
        "request_digest": request_digest,
        "reason_code": "PROMOTION_APPROVAL_DENIED_OR_EXPIRED",
    }
    assert explanation["final_outcome"]["next_action"] == (
        "obtain a fresh digest-bound promotion approval before promotion"
    )
    assert explanation["final_outcome"]["disposition"] == ("promotion_blocked_or_incomplete")
