from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_employee.cli import build_parser
from ai_employee.domain.v2 import DecisionOutcome, PolicyDecision
from ai_employee.orchestration import WorkRun
from ai_employee.run_explanation import explain_any_run
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
