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
from ai_employee.domain.models import NodeResourceBudget
from ai_employee.domain.v2 import CriterionEvidence, WorkerRequest, WorkerResult
from ai_employee.graph import GraphValidationError, accept_task_graph
from ai_employee.services_v2._common import identifier
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    NodeExecutionResult,
    NodeReservationRecord,
    TaskOrchestrator,
    one_node_graph,
)

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
        assert tuple(item.node_id for item in requests["c"].predecessor_outputs) == ("a", "b")
        assert all(item.evaluator_id for item in requests["c"].predecessor_outputs)
        assert tuple(item.worker_result_digest for item in requests["c"].predecessor_outputs) == (
            requests["c"].prior_result_digests
        )
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


def test_explicit_breadth_objective_propagates_to_worker_request(tmp_path: Path) -> None:
    objective = "Exhaustively inspect every authentication path and report every defect"
    requests: list[WorkerRequest] = []

    def runner(
        _node_value: Node,
        request: WorkerRequest,
        _strategy_value: ExecutionStrategy,
    ) -> NodeExecutionResult:
        requests.append(request)
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id="worker-result-audit",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or ZERO,
                status="succeeded",
                duration_seconds=0.01,
            ),
            criterion_evidence=(
                CriterionEvidence(
                    criterion_id="criterion-audit",
                    disposition="satisfied",
                    evidence_refs=(ZERO,),
                ),
            ),
        )

    node = Node(
        id="audit",
        kind=NodeKind.FUNCTION,
        name="Audit authentication",
        objective=objective,
        output_contract=OutputContract(id="contract-audit"),
        required_capabilities=("process",),
        completion_criteria=(
            CompletionCriterion(
                id="criterion-audit",
                description="every authentication path is inspected",
            ),
        ),
    )
    graph = Graph(
        id="audit-graph",
        nodes=(node,),
        entry_node_ids=(node.id,),
        terminal_node_ids=(node.id,),
        budget=Budget(max_attempts=1, max_nodes=1, max_wall_seconds=30.0),
    )
    goal = Goal(id="goal-audit", statement="Complete the accepted exhaustive audit")

    with SQLiteStore(tmp_path / "objective-propagation.db") as store:
        run = TaskOrchestrator(store, runner, (_strategy(),), max_concurrency=1).run(
            goal,
            graph,
            ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=30.0),
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="objective-propagation-run",
            available_capabilities=("process",),
        )

    assert run.status == "completed"
    assert len(requests) == 1
    assert requests[0].goal == objective
    assert requests[0].accepted_plan_digest == run.accepted_graph_revision_digest
    assert "scope_mode" not in requests[0].model_dump()

    compatibility_goal = Goal(id="goal-compatibility", statement=objective)
    compatibility_graph = one_node_graph(
        compatibility_goal,
        graph_id="compatibility-graph",
        node_id="compatibility-node",
    )
    assert compatibility_graph.nodes[0].objective == objective


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


def test_graph_reservations_are_atomic_across_connections(tmp_path: Path) -> None:
    database = tmp_path / "reservations.db"
    with SQLiteStore(database) as store:
        store.migrate_v2()
    barrier = threading.Barrier(8)

    def reserve(index: int) -> bool:
        with SQLiteStore(database) as store:
            barrier.wait(timeout=5)

            def record(remaining: dict[str, int | float]) -> NodeReservationRecord:
                return NodeReservationRecord(
                    id=identifier("reservation"),
                    run_id="reservation-run",
                    created_at=NOW,
                    node_id=f"node-{index}",
                    accepted_graph_revision_digest=ZERO,
                    generation=0,
                    attempt=0,
                    requested={
                        "worker_turns": 1,
                        "processes": 1,
                        "wall_seconds": 1.0,
                        "artifact_bytes": 10,
                        "node_attempts": 1,
                    },
                    remaining_budgets=remaining,
                )

            return store.reserve_graph_node(
                "reservation-run",
                f"node-{index}",
                0,
                0,
                max_claims=3,
                worker_turns=1,
                processes=1,
                wall_seconds=1.0,
                artifact_bytes=10,
                limits={
                    "worker_turns": 3,
                    "processes": 3,
                    "wall_seconds": 3.0,
                    "artifact_bytes": 30,
                },
                record_factory=record,
            ) is not None

    with ThreadPoolExecutor(max_workers=8) as pool:
        reserved = tuple(pool.map(reserve, range(8)))

    assert sum(reserved) == 3
    with SQLiteStore(database) as store:
        records = store.list_records(
            "node_reservation_v2", NodeReservationRecord, run_id="reservation-run"
        )
        assert len(records) == 3
        assert len({(item.node_id, item.generation, item.attempt) for item in records}) == 3


def test_pause_drains_fork_and_resume_preserves_exact_pass_bindings(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pause.db"
    started = {name: threading.Event() for name in ("a", "b")}
    release = threading.Event()
    calls: list[tuple[str, WorkerRequest]] = []
    call_lock = threading.Lock()

    def runner(
        node: Node,
        request: WorkerRequest,
        _strategy_value: ExecutionStrategy,
    ) -> NodeExecutionResult:
        with call_lock:
            calls.append((node.id, request))
        if node.id in started:
            started[node.id].set()
            peer = "b" if node.id == "a" else "a"
            assert started[peer].wait(timeout=3)
            assert release.wait(timeout=3)
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
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id=f"worker-result-{node.id}-{request.generation}",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or ZERO,
                status="succeeded",
                duration_seconds=0.01,
            ),
            criterion_evidence=tuple(criteria),
        )

    def pause_after_dispatch() -> None:
        assert started["a"].wait(timeout=3)
        assert started["b"].wait(timeout=3)
        with SQLiteStore(database) as controller:
            controller.request_control("pause-run", "pause")
        release.set()

    goal = Goal(
        id="goal-pause",
        statement="pause and resume a fork join",
        completion_criteria=(_criterion("goal"),),
    )
    policy = ExecutionPolicy(max_nodes=3, max_attempts=3, max_wall_seconds=30.0)
    controller = threading.Thread(target=pause_after_dispatch)
    with SQLiteStore(database) as store:
        orchestrator = TaskOrchestrator(
            store,
            runner,
            (_strategy(),),
            max_concurrency=2,
        )
        controller.start()
        paused = orchestrator.run(
            goal,
            _fork_join_graph(),
            policy,
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="pause-run",
            available_capabilities=("process",),
        )
        controller.join(timeout=3)
        assert not controller.is_alive()
        assert paused.status == "paused"
        assert paused.failure_code == "GRAPH_PAUSED"
        assert {name for name, _request in calls} == {"a", "b"}
        paused_replay = orchestrator.replay(paused.id)
        paused_by_node = {item.node_id: item for item in paused_replay.nodes}
        assert paused_by_node["a"].status == "passed"
        assert paused_by_node["b"].status == "passed"
        assert paused_by_node["c"].status == "pending"

        resumed = orchestrator.run(
            goal,
            _fork_join_graph(),
            policy,
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="pause-run",
            available_capabilities=("process",),
            resume=True,
        )
        replay = orchestrator.replay(resumed.id)

    assert resumed.status == "completed"
    assert resumed.generation == 1
    assert [name for name, _request in calls].count("a") == 1
    assert [name for name, _request in calls].count("b") == 1
    assert [name for name, _request in calls].count("c") == 1
    join_request = next(request for name, request in calls if name == "c")
    assert join_request.generation == 1
    assert {item.generation for item in join_request.predecessor_outputs} == {1}
    assert {item.result_generation for item in join_request.predecessor_outputs} == {0}
    assert all(item.evaluator_id for item in join_request.predecessor_outputs)
    assert [(item.action, item.generation) for item in replay.controls] == [
        ("pause", 0),
        ("resume", 1),
    ]


def test_cancel_fences_late_results_and_replays_only_stale_control_facts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cancel.db"
    started = {name: threading.Event() for name in ("a", "b")}
    release = threading.Event()
    calls: list[str] = []

    def runner(
        node: Node,
        request: WorkerRequest,
        _strategy_value: ExecutionStrategy,
    ) -> NodeExecutionResult:
        calls.append(node.id)
        started[node.id].set()
        peer = "b" if node.id == "a" else "a"
        assert started[peer].wait(timeout=3)
        assert release.wait(timeout=3)
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id=f"late-result-{node.id}",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or ZERO,
                status="succeeded",
                duration_seconds=0.01,
            ),
            criterion_evidence=(
                CriterionEvidence(
                    criterion_id=f"criterion-{node.id}",
                    disposition="satisfied",
                    evidence_refs=(ZERO,),
                ),
            ),
        )

    def cancel_after_dispatch() -> None:
        assert started["a"].wait(timeout=3)
        assert started["b"].wait(timeout=3)
        with SQLiteStore(database) as controller:
            controller.request_control("cancel-run", "cancel")
        release.set()

    controller = threading.Thread(target=cancel_after_dispatch)
    with SQLiteStore(database) as store:
        orchestrator = TaskOrchestrator(
            store,
            runner,
            (_strategy(),),
            max_concurrency=2,
        )
        controller.start()
        cancelled = orchestrator.run(
            Goal(id="cancel-goal", statement="cancel active work"),
            _fork_join_graph(),
            ExecutionPolicy(max_nodes=3, max_attempts=3, max_wall_seconds=30.0),
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="cancel-run",
            available_capabilities=("process",),
        )
        controller.join(timeout=3)
        replay = orchestrator.replay(cancelled.id)
        assert store.list_records("goal_evaluator_v2", WorkerResult, run_id=cancelled.id) == ()

    assert not controller.is_alive()
    assert cancelled.status == "cancelled"
    assert cancelled.generation == 1
    assert set(calls) == {"a", "b"}
    statuses = {item.node_id: item.status for item in replay.nodes}
    assert statuses == {"a": "cancelled", "b": "cancelled", "c": "cancelled"}
    assert replay.results == ()
    assert replay.evidence == ()
    assert replay.evaluator_decisions == ()
    assert len(replay.stale_results) == 2
    assert {item.result_generation for item in replay.stale_results} == {0}
    assert {item.authoritative_generation for item in replay.stale_results} == {1}
    assert [(item.action, item.generation) for item in replay.controls] == [("cancel", 1)]
    assert replay.worker_invocations == 0
    assert replay.verification_invocations == 0
    assert replay.composition_invocations == 0
    assert replay.promotion_invocations == 0


def test_worker_boundary_retries_once_with_new_attempt_authority(
    tmp_path: Path,
) -> None:
    requests: list[WorkerRequest] = []

    def runner(
        node: Node,
        request: WorkerRequest,
        _strategy_value: ExecutionStrategy,
    ) -> NodeExecutionResult:
        requests.append(request)
        if request.attempt == 0:
            raise RuntimeError("transient worker boundary")
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id=f"worker-result-{request.attempt}",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or ZERO,
                status="succeeded",
                duration_seconds=0.01,
            ),
            criterion_evidence=(
                CriterionEvidence(
                    criterion_id="criterion-only",
                    disposition="satisfied",
                    evidence_refs=(ZERO,),
                ),
            ),
        )

    node = _node("only").model_copy(
        update={
            "retry_limit": 1,
            "resource_budget": NodeResourceBudget(
                worker_turns=1,
                processes=1,
                wall_seconds=1.0,
                artifact_bytes=10,
            ),
        }
    )
    graph = Graph(
        id="retry-graph",
        nodes=(node,),
        entry_node_ids=(node.id,),
        terminal_node_ids=(node.id,),
        budget=Budget(
            max_attempts=2,
            max_retries=1,
            max_nodes=1,
            max_wall_seconds=2.0,
            max_worker_turns=2,
            max_processes=2,
            max_artifact_bytes=20,
        ),
    )
    with SQLiteStore(tmp_path / "retry.db") as store:
        orchestrator = TaskOrchestrator(store, runner, (_strategy(),), max_concurrency=1)
        run = orchestrator.run(
            Goal(id="retry-goal", statement="retry one boundary"),
            graph,
            ExecutionPolicy(max_nodes=1, max_attempts=2, max_wall_seconds=2.0),
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="retry-run",
            available_capabilities=("process",),
        )
        replay = orchestrator.replay(run.id)

    assert run.status == "completed"
    assert [item.attempt for item in requests] == [0, 1]
    assert len({item.id for item in requests}) == 2
    assert len({item.content_digest for item in requests}) == 2
    assert len({item.run_id for item in requests}) == 2
    assert [(item.generation, item.attempt) for item in replay.reservations] == [(0, 0), (0, 1)]
    assert [(item.generation, item.attempt) for item in replay.routes] == [(0, 0), (0, 1)]
    assert len({item.id for item in replay.routes}) == 2


@pytest.mark.parametrize("failure_kind", ["evaluator", "budget"])
def test_evaluator_failure_and_exhausted_resources_do_not_retry(
    tmp_path: Path, failure_kind: str
) -> None:
    requests: list[WorkerRequest] = []

    def runner(
        node: Node,
        request: WorkerRequest,
        _strategy_value: ExecutionStrategy,
    ) -> NodeExecutionResult:
        requests.append(request)
        if failure_kind == "budget":
            raise RuntimeError("retry budget exhausted")
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id="evaluator-fail-result",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or ZERO,
                status="succeeded",
                duration_seconds=0.01,
            ),
            criterion_evidence=(
                CriterionEvidence(
                    criterion_id="criterion-only",
                    disposition="blocked",
                ),
            ),
        )

    node = _node("only").model_copy(update={"retry_limit": 1})
    graph = Graph(
        id=f"no-retry-{failure_kind}",
        nodes=(node,),
        entry_node_ids=(node.id,),
        terminal_node_ids=(node.id,),
        budget=Budget(
            max_attempts=2,
            max_retries=1,
            max_nodes=1,
            max_wall_seconds=2.0,
            max_worker_turns=1 if failure_kind == "budget" else 2,
            max_processes=1 if failure_kind == "budget" else 2,
            max_artifact_bytes=(
                1_000_000 if failure_kind == "budget" else 2_000_000
            ),
        ),
    )
    with SQLiteStore(tmp_path / f"{failure_kind}.db") as store:
        orchestrator = TaskOrchestrator(store, runner, (_strategy(),), max_concurrency=1)
        run = orchestrator.run(
            Goal(id=f"{failure_kind}-goal", statement="do not retry"),
            graph,
            ExecutionPolicy(max_nodes=1, max_attempts=2, max_wall_seconds=2.0),
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id=f"{failure_kind}-run",
            available_capabilities=("process",),
        )
        replay = orchestrator.replay(run.id)

    assert run.status == "failed"
    assert len(requests) == 1
    assert len(replay.routes) == 1
    assert len(replay.reservations) == 1
