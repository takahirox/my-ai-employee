from datetime import UTC, datetime

import pytest

from ai_employee.domain.v2 import CriterionEvidence, WorkerResult
from ai_employee.run_ownership import RunLeaseClosureRecord
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import GraphRunRecord, NodeExecutionResult, TaskOrchestrator
from tests.test_verified_routing_history import GOAL, GRAPH, POLICY, STRATEGIES, ZERO


@pytest.mark.parametrize("boundary", ["consume_child_result", "completed", "ready_to_promote"])
def test_cancel_accepted_at_final_publication_cannot_become_success(tmp_path, boundary):
    database = tmp_path / "cancel.db"

    class Store(SQLiteStore):
        def terminalize_owned_graph_run(self, owner, run, closure_factory, **kwargs):
            if run.status == boundary:
                with SQLiteStore(database) as control:
                    control.request_control(run.id, "cancel")
            return super().terminalize_owned_graph_run(owner, run, closure_factory, **kwargs)

    class Scheduler(TaskOrchestrator):
        def _assert_run_owner(self, operation):
            if operation == boundary:
                with SQLiteStore(database) as control:
                    control.request_control("run", "cancel")
            return super()._assert_run_owner(operation)

    def runner(node, request, strategy):
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id="result",
                run_id=request.run_id,
                created_at=datetime.now(UTC),
                request_digest=request.content_digest,
                status="succeeded",
                duration_seconds=0.01,
            ),
            criterion_evidence=(
                CriterionEvidence(
                    criterion_id="checked",
                    disposition="satisfied",
                    evidence_refs=(ZERO,),
                ),
            ),
        )

    # Force the parent publication status at the same storage boundary without
    # weakening node result validation or relying on race timing.
    class ParentScheduler(Scheduler):
        def _save_run(self, run):
            if boundary == "ready_to_promote" and run.status == "completed":
                run = run.model_copy(update={"status": "ready_to_promote"})
            return super()._save_run(run)

    with Store(database) as store:
        scheduler = ParentScheduler(store, runner, STRATEGIES)
        result = scheduler.run(
            GOAL,
            GRAPH,
            POLICY,
            run_id="run",
            harness_digest=ZERO,
            effective_policy_digest=ZERO,
            available_capabilities=("process",),
        )
        assert result.status == "cancelled"
        assert result.failure_code == "GRAPH_CANCELLED"
        assert result.generation == 1
        assert store.get("graph_run_v2", "run", GraphRunRecord) == result
        closures = store.list_records("run_lease_closure_v2", RunLeaseClosureRecord, run_id="run")
        assert len(closures) == 1
        assert closures[0].terminal_graph_status == "cancelled"
        assert store.current_run_owner("run")["status"] == "closed"
