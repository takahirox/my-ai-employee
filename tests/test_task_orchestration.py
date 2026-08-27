from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_employee.domain import (
    Budget,
    CompletionCriterion,
    Edge,
    ExecutionPolicy,
    ExecutionStrategy,
    Goal,
    Graph,
    Node,
    NodeKind,
    OutputContract,
    RoutingMode,
)
from ai_employee.domain.v2 import CriterionEvidence, WorkerRequest, WorkerResult
from ai_employee.graph import GraphValidationError, accept_task_graph
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import NodeExecutionResult, TaskOrchestrator

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = "0" * 64


def _criterion(name: str) -> CompletionCriterion:
    return CompletionCriterion(id=f"criterion-{name}", description=f"{name} is complete")


def _node(name: str) -> Node:
    return Node(
        id=name,
        kind=NodeKind.FUNCTION,
        name=name,
        objective=f"complete node {name}",
        output_contract=OutputContract(id=f"contract-{name}"),
        required_capabilities=("process",),
        completion_criteria=(_criterion(name),),
        complexity=2 if name == "a" else 3,
        scale=1,
        risk=1,
    )


def _fork_join_graph() -> Graph:
    return Graph(
        id="fork-join",
        nodes=(_node("a"), _node("b"), _node("c")),
        edges=(
            Edge(id="a-c", source_id="a", target_id="c"),
            Edge(id="b-c", source_id="b", target_id="c"),
        ),
        entry_node_ids=("a", "b"),
        terminal_node_ids=("c",),
        budget=Budget(max_attempts=3, max_nodes=3, max_wall_seconds=30.0),
    )


def _strategy() -> ExecutionStrategy:
    return ExecutionStrategy(
        id="strategy-process",
        routing_mode=RoutingMode.ADAPTIVE,
        backend="scripted",
        model="fixture",
        effort="bounded",
        capabilities=("process",),
    )


def test_parallel_three_node_fork_join_persists_and_replays(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    requests: dict[str, WorkerRequest] = {}
    starts: dict[str, float] = {}
    finishes: dict[str, float] = {}
    clock = __import__("time").monotonic

    def runner(
        node: Node,
        request: WorkerRequest,
        _strategy_value: ExecutionStrategy,
    ) -> NodeExecutionResult:
        with lock:
            assert node.id not in requests
            requests[node.id] = request
            starts[node.id] = clock()
        if node.id in {"a", "b"}:
            barrier.wait(timeout=3)
        else:
            assert set(finishes) == {"a", "b"}
        with lock:
            finishes[node.id] = clock()
        criteria = [
            CriterionEvidence(
                criterion_id=f"criterion-{node.id}",
                disposition="satisfied",
                evidence_refs=(ZERO,),
            )
        ]
        if node.id == "c":
            criteria.append(
                CriterionEvidence(
                    criterion_id="criterion-goal",
                    disposition="satisfied",
                    evidence_refs=(ZERO,),
                )
            )
        worker_result = WorkerResult(
            id=f"worker-result-{node.id}",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or ZERO,
            status="succeeded",
            duration_seconds=0.01,
        )
        return NodeExecutionResult(
            worker_result=worker_result,
            criterion_evidence=tuple(criteria),
            workspace_id=f"workspace-{node.id}",
        )

    goal = Goal(
        id="goal-fork-join",
        statement="run two independent tasks and join them",
        completion_criteria=(_criterion("goal"),),
    )
    policy = ExecutionPolicy(max_nodes=3, max_attempts=3, max_wall_seconds=30.0)
    with SQLiteStore(tmp_path / "fleet.db") as store:
        orchestrator = TaskOrchestrator(
            store,
            runner,
            (_strategy(),),
            max_concurrency=2,
        )
        run = orchestrator.run(
            goal,
            _fork_join_graph(),
            policy,
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="fork-join-run",
            available_capabilities=("process",),
        )

        assert run.status == "completed"
        assert set(requests) == {"a", "b", "c"}
        assert len({request.content_digest for request in requests.values()}) == 3
        assert len({request.node_id for request in requests.values()}) == 3
        graph_digests = {
            request.accepted_graph_revision_digest for request in requests.values()
        }
        assert graph_digests == {run.accepted_graph_revision_digest}
        assert starts["c"] >= max(finishes["a"], finishes["b"])
        assert starts["a"] <= finishes["b"] and starts["b"] <= finishes["a"]
        assert store.graph_claims(run.id) == ("a", "b", "c")
        assert not store.claim_graph_node(run.id, "a", max_claims=3)
        assert not store.claim_graph_node(run.id, "extra", max_claims=3)

        replay = orchestrator.replay(run.id)
        assert replay.worker_invocations == 0
        assert replay.run == run
        assert tuple(item.status for item in replay.nodes) == ("passed", "passed", "passed")
        assert replay.route_count == 3
        assert replay.worker_result_count == 3
        assert replay.evidence_count == 3
        assert replay.evaluator_count == 3


def test_task_graph_acceptance_rejects_cycles_general_edges_and_tight_budget() -> None:
    graph = _fork_join_graph()
    policy = ExecutionPolicy(max_nodes=3, max_attempts=3, max_wall_seconds=30.0)
    accepted = accept_task_graph(graph, policy, available_capabilities=("process",))
    assert accepted.revision_number == 1

    conditional = graph.model_copy(
        update={
            "edges": (
                graph.edges[0].model_copy(update={"condition": "succeeded"}),
                graph.edges[1],
            )
        }
    )
    with pytest.raises(GraphValidationError) as caught:
        accept_task_graph(conditional, policy, available_capabilities=("process",))
    assert "unsupported_edge_semantics" in {
        item.code for item in caught.value.issues
    }

    tight = graph.model_copy(
        update={"budget": graph.budget.model_copy(update={"max_attempts": 2})}
    )
    with pytest.raises(GraphValidationError) as caught:
        accept_task_graph(tight, policy, available_capabilities=("process",))
    assert "attempt_budget_insufficient" in {item.code for item in caught.value.issues}


def test_graph_claim_budget_is_atomic_across_connections(tmp_path: Path) -> None:
    database = tmp_path / "claims.db"
    with SQLiteStore(database) as store:
        store.migrate_v2()

    barrier = threading.Barrier(8)

    def claim(node_id: str) -> bool:
        with SQLiteStore(database) as store:
            barrier.wait(timeout=3)
            return store.claim_graph_node("run", node_id, max_claims=3)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = tuple(pool.map(claim, (f"node-{index}" for index in range(8))))

    assert sum(claimed) == 3
    with SQLiteStore(database) as store:
        assert len(store.graph_claims("run")) == 3
