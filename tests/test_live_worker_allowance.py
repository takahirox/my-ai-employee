from datetime import UTC, datetime

import pytest

from ai_employee.domain import ExecutionPolicy, ExecutionStrategy, Goal, RoutingMode
from ai_employee.domain.v2 import CriterionEvidence, WorkerRequest, WorkerResult
from ai_employee.run_budget import wall_budget_scope
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    NodeExecutionResult,
    TaskOrchestrator,
    WorkerContextManifest,
    one_node_graph,
)
from ai_employee.worker_supervision import WorkerTimeoutProfileRecord


@pytest.mark.parametrize(("adapter_timeout", "expected"), [(None, 81.0), (40.0, 40.0)])
def test_worker_and_persisted_context_receive_actual_attempt_allowance(
    tmp_path, adapter_timeout, expected
):
    goal = Goal(id="goal", statement="produce a bounded result")
    graph = one_node_graph(
        goal,
        graph_id="graph",
        node_id="node",
        max_wall_seconds=173.0,
        required_capabilities=("process",),
    )
    strategy = ExecutionStrategy(
        id="fixture",
        routing_mode=RoutingMode.FIXED,
        backend="scripted",
        model="fixture",
        capabilities=("process",),
    )
    received = []
    clock = [0.0]

    def runner(node, request, selected):
        received.append(request)
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id="result",
                run_id=request.run_id,
                created_at=datetime.now(UTC),
                request_digest=request.content_digest,
                status="succeeded",
                duration_seconds=0.0,
            ),
            criterion_evidence=(
                CriterionEvidence(
                    criterion_id=node.completion_criteria[0].id,
                    disposition="satisfied",
                    evidence_refs=("a" * 64,),
                ),
            ),
        )

    with SQLiteStore(tmp_path / "state.db") as store:
        with wall_budget_scope(store, "run", 173.0, clock=lambda: clock[0]):
            clock[0] = 90.0
            result = TaskOrchestrator(
                store,
                runner,
                (strategy,),
                routing_mode=RoutingMode.FIXED,
                fixed_strategy_id=strategy.id,
                adapter_timeout_seconds=adapter_timeout,
            ).run(
                goal,
                graph,
                ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=173.0),
                harness_digest="a" * 64,
                effective_policy_digest="b" * 64,
                run_id="run",
                available_capabilities=("process",),
            )
        assert result.status == "completed"
    with SQLiteStore(tmp_path / "state.db") as store:
        requests = store.list_records("worker_request_v2", WorkerRequest, run_id="run")
        contexts = store.list_records(
            "worker_context_manifest_v2", WorkerContextManifest, run_id="run"
        )
        profiles = store.list_records(
            "worker_timeout_profile_v2", WorkerTimeoutProfileRecord, run_id="run"
        )
    assert len(received) == len(requests) == len(contexts) == len(profiles) == 1
    assert received[0].remaining_budgets["wall_seconds"] == expected
    assert (
        requests[0].remaining_budgets
        == contexts[0].remaining_budgets
        == received[0].remaining_budgets
    )
    assert profiles[0].effective_timeout_seconds == expected
    assert profiles[0].accepted_node_timeout_seconds == 173.0
    assert received[0].remaining_budgets["processes"] == graph.nodes[0].resource_budget.processes
