from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ai_employee.domain import (
    Budget,
    CompletionCriterion,
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
from ai_employee.inspector import inspect_graph_run
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    GraphRunRecord,
    LoopAction,
    NodeExecutionResult,
    NodeRunner,
    TaskOrchestrator,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = "0" * 64
HARNESS = "1" * 64
POLICY = "2" * 64


def _strategy() -> ExecutionStrategy:
    return ExecutionStrategy(
        id="closed-loop-fixture",
        routing_mode=RoutingMode.ADAPTIVE,
        backend="scripted",
        model="fixture",
        effort="bounded",
        capabilities=("process",),
    )


def _inputs(
    *,
    max_retries: int = 0,
    max_repairs: int = 0,
) -> tuple[Goal, Graph, Node]:
    criterion = CompletionCriterion(id="criterion-fix", description="the defect is fixed")
    node = Node(
        id="fix",
        kind=NodeKind.FUNCTION,
        name="fix",
        objective="fix the bounded defect",
        output_contract=OutputContract(id="contract-fix"),
        required_capabilities=("process",),
        completion_criteria=(criterion,),
        retry_limit=max_retries,
    )
    graph = Graph(
        id="closed-loop-graph",
        nodes=(node,),
        entry_node_ids=(node.id,),
        terminal_node_ids=(node.id,),
        budget=Budget(
            max_attempts=4,
            max_retries=max_retries,
            max_repairs=max_repairs,
            max_nodes=1,
            max_worker_turns=4,
            max_processes=4,
            max_wall_seconds=4.0,
        ),
    )
    goal = Goal(id="closed-loop-goal", statement="complete bounded improvement")
    return goal, graph, node


def _result(request: WorkerRequest, disposition: str) -> NodeExecutionResult:
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
                criterion_id="criterion-fix",
                disposition=disposition,
                evidence_refs=(ZERO,),
            ),
        ),
    )


def _run(
    store: SQLiteStore,
    graph: Graph,
    goal: Goal,
    runner: NodeRunner,
    *,
    run_id: str,
) -> tuple[TaskOrchestrator, GraphRunRecord]:
    orchestrator = TaskOrchestrator(store, runner, (_strategy(),))
    result = orchestrator.run(
        goal,
        graph,
        ExecutionPolicy(max_nodes=1, max_attempts=4, max_wall_seconds=4.0),
        harness_digest=HARNESS,
        effective_policy_digest=POLICY,
        run_id=run_id,
        available_capabilities=("process",),
    )
    return orchestrator, result


def test_transient_failure_retries_once_then_passes(tmp_path: Path) -> None:
    goal, graph, _node = _inputs(max_retries=1)
    requests: list[WorkerRequest] = []

    def runner(
        _bound_node: Node,
        request: WorkerRequest,
        _selected: ExecutionStrategy,
    ) -> NodeExecutionResult:
        requests.append(request)
        if request.attempt == 0:
            raise RuntimeError("transient")
        return _result(request, "satisfied")

    with SQLiteStore(tmp_path / "retry.db") as store:
        orchestrator, run = _run(store, graph, goal, runner, run_id="closed-loop-retry")
        replay = orchestrator.replay("closed-loop-retry")

    assert run.status == "completed"
    assert [item.action for item in replay.loop_transitions] == [
        LoopAction.RETRY,
        LoopAction.PASS,
    ]
    assert [request.attempt for request in requests] == [0, 1]
    assert all(not request.accepted_feedback_digests for request in requests)


def test_failed_evaluation_repairs_with_fresh_bound_feedback(tmp_path: Path) -> None:
    goal, graph, node = _inputs(max_repairs=1)
    requests: list[WorkerRequest] = []

    def runner(
        _bound_node: Node,
        request: WorkerRequest,
        _selected: ExecutionStrategy,
    ) -> NodeExecutionResult:
        requests.append(request)
        return _result(request, "blocked" if request.attempt == 0 else "satisfied")

    with SQLiteStore(tmp_path / "repair.db") as store:
        orchestrator, run = _run(store, graph, goal, runner, run_id="closed-loop-repair")
        replay = orchestrator.replay("closed-loop-repair")
        inspected = inspect_graph_run(store, "closed-loop-repair")

        assert run.status == "completed"
        assert [item.action for item in replay.loop_transitions] == [
            LoopAction.REPAIR,
            LoopAction.PASS,
        ]
        repair = replay.loop_transitions[0]
        assert requests[0].accepted_feedback_digests == ()
        assert requests[1].accepted_feedback_digests == repair.evidence_digests
        assert len(repair.evidence_digests) == 2
        manifest = replay.context_manifests[-1]
        assert manifest.accepted_feedback_digests == repair.evidence_digests
        assert not manifest.conversation_history_included
        assert not manifest.artifact_bodies_included
        assert inspected["loop_transitions"][0]["action"] == "REPAIR"

        stale_generation = requests[1].model_copy(update={"generation": 1})
        stale_revision = requests[1].model_copy(
            update={
                "accepted_plan_digest": "3" * 64,
                "accepted_graph_revision_digest": "3" * 64,
            }
        )
        assert not orchestrator._repair_feedback_is_authoritative(
            node.model_copy(update={"generation": 1, "attempt": 1}), stale_generation
        )
        assert not orchestrator._repair_feedback_is_authoritative(
            node.model_copy(update={"attempt": 1}), stale_revision
        )


def test_repair_bound_exhaustion_escalates_and_fails_closed(tmp_path: Path) -> None:
    goal, graph, _node = _inputs(max_repairs=1)

    def runner(
        _bound_node: Node,
        request: WorkerRequest,
        _selected: ExecutionStrategy,
    ) -> NodeExecutionResult:
        return _result(request, "blocked")

    with SQLiteStore(tmp_path / "exhausted.db") as store:
        orchestrator, run = _run(store, graph, goal, runner, run_id="closed-loop-exhausted")
        replay = orchestrator.replay("closed-loop-exhausted")

    assert run.status == "failed"
    assert [item.action for item in replay.loop_transitions] == [
        LoopAction.REPAIR,
        LoopAction.ESCALATE,
    ]
    assert replay.loop_transitions[-1].reason_code == "REPAIR_BUDGET_EXHAUSTED"
    assert replay.nodes[0].failure_code == "LOOP_ESCALATED:REPAIR_BUDGET_EXHAUSTED"


def test_retry_and_repair_have_independent_budgets(tmp_path: Path) -> None:
    goal, graph, _node = _inputs(max_retries=1, max_repairs=1)

    def runner(
        _bound_node: Node,
        request: WorkerRequest,
        _selected: ExecutionStrategy,
    ) -> NodeExecutionResult:
        if request.attempt == 0:
            raise RuntimeError("transient")
        return _result(request, "blocked" if request.attempt == 1 else "satisfied")

    with SQLiteStore(tmp_path / "independent.db") as store:
        orchestrator, run = _run(store, graph, goal, runner, run_id="closed-loop-independent")
        replay = orchestrator.replay("closed-loop-independent")

    assert run.status == "completed"
    assert [item.action for item in replay.loop_transitions] == [
        LoopAction.RETRY,
        LoopAction.REPAIR,
        LoopAction.PASS,
    ]
    assert replay.loop_transitions[0].consumed == 1
    assert replay.loop_transitions[1].consumed == 1


def test_non_repairable_evaluation_selects_terminal_fail(tmp_path: Path) -> None:
    goal, graph, _node = _inputs()

    def runner(
        _bound_node: Node,
        request: WorkerRequest,
        _selected: ExecutionStrategy,
    ) -> NodeExecutionResult:
        return _result(request, "blocked")

    with SQLiteStore(tmp_path / "fail.db") as store:
        orchestrator, run = _run(store, graph, goal, runner, run_id="closed-loop-fail")
        replay = orchestrator.replay("closed-loop-fail")

    assert run.status == "failed"
    assert [item.action for item in replay.loop_transitions] == [LoopAction.FAIL]
    assert replay.loop_transitions[0].reason_code == "NODE_EVALUATION_NOT_PASS"


def test_pending_repair_feedback_survives_pause_and_resume(tmp_path: Path) -> None:
    goal, graph, _node = _inputs(max_repairs=1)
    database = tmp_path / "repair-resume.db"
    requests: list[WorkerRequest] = []

    def runner(
        _bound_node: Node,
        request: WorkerRequest,
        _selected: ExecutionStrategy,
    ) -> NodeExecutionResult:
        requests.append(request)
        if request.generation == 0:
            with SQLiteStore(database) as controller:
                controller.request_control("closed-loop-repair-resume", "pause")
            return _result(request, "blocked")
        return _result(request, "satisfied")

    with SQLiteStore(database) as store:
        orchestrator, paused = _run(
            store,
            graph,
            goal,
            runner,
            run_id="closed-loop-repair-resume",
        )
        assert paused.status == "paused"
        resumed = orchestrator.run(
            goal,
            graph,
            ExecutionPolicy(max_nodes=1, max_attempts=4, max_wall_seconds=4.0),
            harness_digest=HARNESS,
            effective_policy_digest=POLICY,
            run_id="closed-loop-repair-resume",
            available_capabilities=("process",),
            resume=True,
        )
        replay = orchestrator.replay("closed-loop-repair-resume")

    repair = next(item for item in replay.loop_transitions if item.action is LoopAction.REPAIR)
    assert resumed.status == "completed"
    assert [(item.generation, item.attempt) for item in requests] == [(0, 0), (1, 1)]
    assert requests[1].accepted_feedback_digests == repair.evidence_digests
    assert [item.action for item in replay.loop_transitions] == [
        LoopAction.REPAIR,
        LoopAction.PASS,
    ]


def test_repair_feedback_survives_worker_retry(tmp_path: Path) -> None:
    goal, graph, _node = _inputs(max_retries=1, max_repairs=1)
    requests: list[WorkerRequest] = []

    def runner(
        _bound_node: Node,
        request: WorkerRequest,
        _selected: ExecutionStrategy,
    ) -> NodeExecutionResult:
        requests.append(request)
        if request.attempt == 0:
            return _result(request, "blocked")
        if request.attempt == 1:
            raise RuntimeError("transient repair worker failure")
        return _result(request, "satisfied")

    with SQLiteStore(tmp_path / "repair-retry.db") as store:
        orchestrator, run = _run(store, graph, goal, runner, run_id="closed-loop-repair-retry")
        replay = orchestrator.replay("closed-loop-repair-retry")

    repair = replay.loop_transitions[0]
    assert run.status == "completed"
    assert [item.action for item in replay.loop_transitions] == [
        LoopAction.REPAIR,
        LoopAction.RETRY,
        LoopAction.PASS,
    ]
    assert [item.accepted_feedback_digests for item in requests] == [
        (),
        repair.evidence_digests,
        repair.evidence_digests,
    ]
