from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_employee.domain import (
    Budget,
    CompletionCriterion,
    Edge,
    ExecutionPolicy,
    ExecutionStrategy,
    Goal,
    GoalTaskKind,
    Graph,
    Node,
    NodeKind,
    OutputContract,
    RoutingMode,
    SemanticAmbiguity,
    SemanticReasoningClass,
    SemanticScope,
    SemanticTaskProfile,
    SemanticTaskType,
)
from ai_employee.domain.models import NodeResourceBudget
from ai_employee.domain.v2 import (
    CriterionEvidence,
    StableFailure,
    StableFailureCode,
    WorkerBoundaryDiagnostic,
    WorkerRequest,
    WorkerResult,
)
from ai_employee.graph import GraphValidationError, accept_task_graph
from ai_employee.inspector import inspect_any_run, inspect_graph_run
from ai_employee.plan_review import (
    PlanReviewAcceptanceBinding,
    PlanReviewAttempt,
    PlanReviewFailureEvidence,
    PlanReviewFailureKind,
    PlanReviewFinding,
    PlanReviewFindingType,
    PlanReviewGateError,
    PlanReviewImpact,
    PlanReviewInvocationError,
    PlanReviewPayload,
    PlanRevisionAttempt,
    bind_plan_review,
)
from ai_employee.run_explanation import explain_any_run
from ai_employee.serialization import canonical_digest
from ai_employee.services_v2._common import identifier
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    NodeControlPropagationRecord,
    NodeExecutionRecord,
    NodeExecutionResult,
    NodeReservationRecord,
    NodeRouteRecord,
    NodeWatchdogRecord,
    PreAcceptanceGoalRecord,
    TaskGraphAcceptance,
    TaskOrchestrator,
    one_node_graph,
)
from ai_employee.task_planning import ProposedGraph

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = "0" * 64


def test_scheduler_watchdog_terminalizes_an_overdue_node_attempt(tmp_path: Path) -> None:
    goal = Goal(
        id="watchdog-goal",
        statement="bound the worker",
        completion_criteria=(_criterion("watchdog"),),
    )
    graph = one_node_graph(
        goal,
        graph_id="watchdog-graph",
        node_id="watchdog-node",
        required_capabilities=("process",),
        max_wall_seconds=0.05,
    )

    def slow_runner(
        _node_value: Node,
        request: WorkerRequest,
        _strategy_value: ExecutionStrategy,
    ) -> NodeExecutionResult:
        threading.Event().wait(0.1)
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id="late-watchdog-result",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or ZERO,
                status="succeeded",
                duration_seconds=0.1,
            ),
            criterion_evidence=(),
        )

    with SQLiteStore(tmp_path / "watchdog.db") as store:
        run = TaskOrchestrator(store, slow_runner, (_strategy(),)).run(
            goal,
            graph,
            ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=1.0),
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="watchdog-run",
            available_capabilities=("process",),
        )
        watchdogs = store.list_records("node_watchdog_v2", NodeWatchdogRecord, run_id=run.id)
        replay = TaskOrchestrator(store, slow_runner, (_strategy(),)).replay(run.id)

    assert run.status == "failed"
    assert replay.nodes[0].failure_code == "WATCHDOG_TIMEOUT"
    assert {item.outcome for item in watchdogs} == {"signal_sent", "cleanup_confirmed"}
    assert len({item.child_run_id for item in watchdogs}) == 1
    assert watchdogs[0].child_run_id.startswith("node-")


def test_fixed_compatibility_graph_preserves_non_mutating_contract_and_budget() -> None:
    criterion = CompletionCriterion(
        id="typed-diagnosis",
        description="a typed diagnosis is accepted",
    )
    goal = Goal(
        id="non-mutating-goal",
        statement="diagnose only",
        completion_criteria=(criterion,),
        task_kind=GoalTaskKind.NON_MUTATING,
        processes_authorized=False,
    )

    graph = one_node_graph(
        goal,
        graph_id="non-mutating-graph",
        node_id="diagnosis-node",
        required_capabilities=(),
        max_wall_seconds=12.0,
    )

    assert graph.nodes[0].completion_criteria == (criterion,)
    assert graph.nodes[0].required_capabilities == ()
    assert graph.nodes[0].resource_budget.wall_seconds == 12.0
    assert all(
        "workspace_patch" not in item.required_artifact_ids
        for item in graph.nodes[0].completion_criteria
    )

    fallback_graph = one_node_graph(
        goal.model_copy(update={"completion_criteria": ()}),
        graph_id="fallback-non-mutating-graph",
        node_id="fallback-diagnosis-node",
    )
    fallback = fallback_graph.nodes[0].completion_criteria[0]
    assert fallback.id == "criterion-fallback-diagnosis-node"
    assert fallback.source == "accepted_non_mutating_result"
    assert fallback.description == "the node-bound worker result is accepted"


def test_mutating_compatibility_graph_reserves_one_bounded_repair() -> None:
    criterion = CompletionCriterion(
        id="verified-edit",
        description="the edit is verified",
        verification_requirement_ids=("test", "lint", "typecheck"),
    )
    graph = one_node_graph(
        Goal(
            id="mutating-goal",
            statement="make one verified edit",
            completion_criteria=(criterion,),
        ),
        graph_id="mutating-graph",
        node_id="mutating-node",
        required_capabilities=("edit_intent", "process"),
        max_wall_seconds=12.0,
    )

    node = graph.nodes[0]
    assert node.resource_budget.processes == 3
    assert node.resource_budget.wall_seconds == 6.0
    assert graph.budget.max_attempts == 2
    assert graph.budget.max_repairs == 1
    assert graph.budget.max_loop_iterations == 2
    assert graph.budget.max_worker_turns == 2
    assert graph.budget.max_processes == 6
    assert graph.budget.max_wall_seconds == 12.0
    assert graph.budget.max_artifact_bytes == 2_000_000


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
        semantic_profile=SemanticTaskProfile(
            task_type=SemanticTaskType.IMPLEMENTATION,
            reasoning_class=SemanticReasoningClass.MODERATE,
            scope=SemanticScope.LOCAL,
            ambiguity=SemanticAmbiguity.LOW,
            reasons=("bounded orchestration fixture",),
        ),
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


def test_non_mutating_task_dispatches_with_strategy_capability_superset(
    tmp_path: Path,
) -> None:
    dispatched: list[tuple[WorkerRequest, ExecutionStrategy]] = []
    strategy = _strategy().model_copy(update={"capabilities": ("edit_intent", "process")})

    def runner(
        node: Node,
        request: WorkerRequest,
        selected_strategy: ExecutionStrategy,
    ) -> NodeExecutionResult:
        dispatched.append((request, selected_strategy))
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id="non-mutating-dispatch-result",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or ZERO,
                status="succeeded",
                duration_seconds=0.01,
            ),
            criterion_evidence=(
                CriterionEvidence(
                    criterion_id="criterion-diagnosis",
                    disposition="satisfied",
                    evidence_refs=(ZERO,),
                ),
            ),
        )

    goal = Goal(
        id="non-mutating-dispatch-goal",
        statement="return one bounded diagnosis",
        task_kind=GoalTaskKind.NON_MUTATING,
        processes_authorized=False,
        completion_criteria=(_criterion("diagnosis"),),
    )
    graph = one_node_graph(
        goal,
        graph_id="non-mutating-dispatch-graph",
        node_id="non-mutating-dispatch-node",
        required_capabilities=(),
    )
    with SQLiteStore(tmp_path / "non-mutating-dispatch.db") as store:
        run = TaskOrchestrator(store, runner, (strategy,)).run(
            goal,
            graph,
            ExecutionPolicy(max_nodes=1, max_attempts=1),
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="non-mutating-dispatch-run",
            available_capabilities=(),
        )

    assert run.status == "completed"
    assert len(dispatched) == 1
    assert dispatched[0][0].completion_criteria == goal.completion_criteria
    assert dispatched[0][0].required_capabilities == ()
    assert set(dispatched[0][1].capabilities) == {"edit_intent", "process"}


def test_tampered_required_capability_binding_fails_before_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _strategy().model_copy(update={"capabilities": ("edit_intent", "process")})
    original_route = TaskOrchestrator._route

    def tampered_route(
        orchestrator: TaskOrchestrator,
        run_id: str,
        node: Node,
        dependency_ids: tuple[str, ...],
        graph_digest: str,
        effective_policy_digest: str,
        harness_digest: str,
        *,
        independent_node_assessment: bool,
    ) -> NodeRouteRecord:
        route = original_route(
            orchestrator,
            run_id,
            node,
            dependency_ids,
            graph_digest,
            effective_policy_digest,
            harness_digest,
            independent_node_assessment=independent_node_assessment,
        )
        facts = route.routing_facts
        assert facts is not None
        return NodeRouteRecord(
            **route.model_dump(exclude={"content_digest", "routing_facts"}),
            routing_facts=facts.model_copy(
                update={"required_capabilities": ("edit_intent", "process")}
            ),
        )

    runner_calls = 0

    def runner(
        _node: Node,
        _request: WorkerRequest,
        _strategy_value: ExecutionStrategy,
    ) -> NodeExecutionResult:
        nonlocal runner_calls
        runner_calls += 1
        raise AssertionError("tampered bindings must fail before dispatch")

    monkeypatch.setattr(TaskOrchestrator, "_route", tampered_route)
    goal = Goal(
        id="tampered-route-goal",
        statement="reject a stale routed capability binding",
        completion_criteria=(_criterion("tampered-route"),),
    )
    graph = one_node_graph(
        goal,
        graph_id="tampered-route-graph",
        node_id="tampered-route-node",
        required_capabilities=("process",),
    )
    with SQLiteStore(tmp_path / "tampered-route.db") as store:
        run = TaskOrchestrator(store, runner, (strategy,)).run(
            goal,
            graph,
            ExecutionPolicy(max_nodes=1, max_attempts=1),
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="tampered-route-run",
            available_capabilities=("edit_intent", "process"),
        )
        diagnostics = store.list_records(
            "worker_boundary_diagnostic_v2",
            WorkerBoundaryDiagnostic,
            run_id=run.id,
        )

    assert run.status == "failed"
    assert runner_calls == 0
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.stage == "pre_dispatch"
    assert diagnostic.code == (StableFailureCode.WORKER_DISPATCH_CONTRACT_CONTRADICTION.value)
    assert diagnostic.retryable is False
    assert diagnostic.exception_message == (
        "pre-dispatch bindings contradict accepted run authority; recreate the route and worker "
        "request from the persisted task and effective policy"
    )


def test_missing_criterion_evidence_capability_fails_before_runner(tmp_path: Path) -> None:
    runner_calls = 0

    def runner(
        _node: Node,
        _request: WorkerRequest,
        _strategy_value: ExecutionStrategy,
    ) -> NodeExecutionResult:
        nonlocal runner_calls
        runner_calls += 1
        raise AssertionError("an impossible criterion contract must not reach the runner")

    criterion = CompletionCriterion(
        id="criterion-verification",
        description="the required verification passes",
        verification_requirement_ids=("test",),
    )
    node = _node("impossible").model_copy(
        update={
            "required_capabilities": (),
            "completion_criteria": (criterion,),
        }
    )
    graph = Graph(
        id="impossible-criterion-graph",
        nodes=(node,),
        entry_node_ids=(node.id,),
        terminal_node_ids=(node.id,),
        budget=Budget(max_attempts=1, max_nodes=1),
    )
    goal = Goal(id="impossible-criterion-goal", statement="require unavailable evidence")

    with SQLiteStore(tmp_path / "impossible-criterion.db") as store:
        run = TaskOrchestrator(store, runner, (_strategy(),)).run(
            goal,
            graph,
            ExecutionPolicy(max_nodes=1, max_attempts=1),
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="impossible-criterion-run",
            available_capabilities=("process",),
        )
        diagnostics = store.list_records(
            "worker_boundary_diagnostic_v2",
            WorkerBoundaryDiagnostic,
            run_id=run.id,
        )
        requests = store.list_records("worker_request_v2", WorkerRequest, run_id=run.id)

    assert run.status == "failed"
    assert runner_calls == 0
    assert len(requests) == 1
    assert requests[0].completion_criteria == (criterion,)
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.stage == "pre_dispatch"
    assert diagnostic.code == (
        StableFailureCode.WORKER_DISPATCH_CONTRACT_CONTRADICTION.value
    )
    assert "criterion criterion-verification" in (diagnostic.exception_message or "")
    assert "process capability" in (diagnostic.exception_message or "")


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
        assert all(
            request.remaining_budgets
            == {
                "worker_turns": 1,
                "processes": 1,
                "wall_seconds": 1.0,
                "artifact_bytes": 1_000_000,
                "node_attempts": 1,
            }
            for request in requests.values()
        )
        graph_digests = {request.accepted_graph_revision_digest for request in requests.values()}
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
        explanation = explain_any_run(store, run.id)
        assert explanation == explain_any_run(store, run.id)
        assert explanation["goal"]["statement"] == goal.statement
        assert explanation["current_state"]["task_counts"] == {"completed": 3}
        assert explanation["graph"]["accepted"] is True
        assert [item["task_id"] for item in explanation["task_stories"]] == ["a", "b", "c"]
        assert explanation["task_stories"][2]["information_flow"]["predecessor_task_ids"] == [
            "a",
            "b",
        ]
        assert explanation["task_stories"][0]["routing"]["selected_strategy"]["id"] == (
            "strategy-process"
        )
        assert explanation["final_outcome"]["disposition"] == "accepted"
        assert explanation["observation"] == {
            "source": "persisted_facts",
            "read_only": True,
            "ai_invocations": 0,
            "artifact_bodies_read": False,
        }
        assert len(requests) == 3


def test_explanation_marks_ready_and_dependency_waiting_tasks(tmp_path: Path) -> None:
    goal = Goal(id="goal-planned", statement="plan a fork and join")
    with SQLiteStore(tmp_path / "planned.db") as store:
        orchestrator = TaskOrchestrator(
            store,
            lambda *_args: pytest.fail("plan-only explanation must not invoke a Worker"),
            (_strategy(),),
            max_concurrency=2,
        )
        run = orchestrator.run(
            goal,
            _fork_join_graph(),
            ExecutionPolicy(max_nodes=3, max_attempts=3, max_wall_seconds=30.0),
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="planned-explanation",
            available_capabilities=("process",),
            plan_only=True,
        )

        assert run.status == "planned"
        explanation = explain_any_run(store, run.id)
        assert explanation["current_state"]["ready_task_ids"] == ["a", "b"]
        assert explanation["current_state"]["waiting_task_ids"] == ["c"]
        assert explanation["current_state"]["task_counts"] == {"ready": 2, "waiting": 1}
        assert explanation["task_stories"][2]["why_this_state"] == [
            "one or more predecessor tasks have not passed"
        ]


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
    assert "unsupported_edge_semantics" in {item.code for item in caught.value.issues}

    tight = graph.model_copy(update={"budget": graph.budget.model_copy(update={"max_attempts": 2})})
    with pytest.raises(GraphValidationError) as caught:
        accept_task_graph(tight, policy, available_capabilities=("process",))
    assert "attempt_budget_insufficient" in {item.code for item in caught.value.issues}


class _ScriptedPlanReviewer:
    def __init__(
        self,
        strategy: ExecutionStrategy,
        findings: dict[int, tuple[PlanReviewFinding, ...]],
        events: list[str],
    ) -> None:
        self.strategy = strategy
        self.findings = findings
        self.events = events
        self.calls: list[int] = []

    def review(
        self,
        goal: Goal,
        proposed_graph: ProposedGraph,
        *,
        review_round: int,
        available_capabilities: tuple[str, ...],
        max_nodes: int,
        max_wall_seconds: float,
    ):
        assert available_capabilities == ("process",)
        assert max_nodes == 1
        assert max_wall_seconds == 30.0
        self.calls.append(review_round)
        self.events.append(f"review-{review_round}")
        return bind_plan_review(
            PlanReviewPayload(findings=self.findings[review_round]),
            record_id=f"trusted-review-{review_round}",
            run_id=proposed_graph.run_id,
            created_at=NOW,
            review_round=review_round,  # type: ignore[arg-type]
            goal=goal,
            proposed_graph=proposed_graph,
            reviewer_strategy=self.strategy,
        )


class _ScriptedPlanReviser:
    def __init__(
        self,
        strategy: ExecutionStrategy,
        revised_graph: Graph,
        events: list[str],
    ) -> None:
        self.strategy = strategy
        self.revised_graph = revised_graph
        self.events = events
        self.calls = 0

    def revise(
        self,
        goal: Goal,
        original: ProposedGraph,
        blocking_findings: tuple[PlanReviewFinding, ...],
        *,
        available_capabilities: tuple[str, ...],
        max_nodes: int,
        max_wall_seconds: float,
    ) -> ProposedGraph:
        del goal, available_capabilities, max_nodes, max_wall_seconds
        assert len(blocking_findings) == 1
        self.calls += 1
        self.events.append("revision")
        return ProposedGraph.model_validate(
            {
                **original.model_dump(
                    mode="python", exclude={"id", "created_at", "content_digest"}
                ),
                "id": "revised-proposal",
                "created_at": NOW,
                "graph": self.revised_graph,
            },
            strict=True,
        )


class _ScriptedNodeAssessor:
    def __init__(
        self,
        strategy: ExecutionStrategy,
        profile: SemanticTaskProfile,
        events: list[str],
    ) -> None:
        self.strategy = strategy
        self.profile = profile
        self.events = events

    def assess(self, goal: str, _deterministic: object) -> SemanticTaskProfile:
        assert goal == "complete the bounded task"
        self.events.append("assess")
        return self.profile


def _review_proposal(run_id: str, goal: Goal, graph: Graph) -> ProposedGraph:
    return ProposedGraph(
        id=f"original-proposal-{run_id}",
        run_id=run_id,
        created_at=NOW,
        goal_id=goal.id,
        goal_digest=canonical_digest(goal),
        graph=graph,
        planner_strategy=_strategy(),
        effective_policy_digest="1" * 64,
        harness_digest=ZERO,
    )


def _blocking_review_finding(node_id: str = "a") -> PlanReviewFinding:
    return PlanReviewFinding(
        id="finding-a",
        finding_type=PlanReviewFindingType.PREMATURE_GENERALIZATION,
        impact=PlanReviewImpact.BLOCKING,
        affected_node_ids=(node_id,),
        goal_relation="The original objective generalizes beyond the accepted goal.",
        smallest_correction="Use the bounded accepted-goal objective only.",
    )


def test_blocking_review_revises_once_and_persists_acceptance_chain(tmp_path: Path) -> None:
    events: list[str] = []
    goal = Goal(id="review-goal", statement="complete one bounded task")
    original = one_node_graph(
        goal,
        graph_id="over-engineered",
        node_id="a",
        required_capabilities=("process",),
        max_wall_seconds=30.0,
    )
    routed_profile = _node("a").semantic_profile
    original = original.model_copy(
        update={
            "nodes": (
                original.nodes[0].model_copy(
                    update={
                        "semantic_profile": routed_profile,
                        "complexity": 2,
                        "scale": 1,
                    }
                ),
            )
        }
    )
    revised = original.model_copy(
        update={
            "id": "minimal-revision",
            "nodes": (
                original.nodes[0].model_copy(update={"objective": "complete the bounded task"}),
            ),
        }
    )
    finding = _blocking_review_finding()
    reviewer = _ScriptedPlanReviewer(_strategy(), {0: (finding,), 1: ()}, events)
    reviser = _ScriptedPlanReviser(_strategy(), revised, events)

    database = tmp_path / "review-gate.db"
    with SQLiteStore(database) as store:

        def runner(
            _node_value: Node,
            request: WorkerRequest,
            _strategy_value: ExecutionStrategy,
        ) -> NodeExecutionResult:
            with SQLiteStore(database) as reader:
                bindings = reader.list_records(
                    "plan_review_acceptance_binding_v2",
                    PlanReviewAcceptanceBinding,
                    run_id="review-run",
                )
            assert len(bindings) == 1
            events.append("worker")
            return NodeExecutionResult(
                worker_result=WorkerResult(
                    id="review-worker-result",
                    run_id=request.run_id,
                    created_at=NOW,
                    request_digest=request.content_digest or ZERO,
                    status="succeeded",
                    duration_seconds=0.01,
                ),
                criterion_evidence=(
                    CriterionEvidence(
                        criterion_id="criterion-a",
                        disposition="satisfied",
                        evidence_refs=(ZERO,),
                    ),
                ),
            )

        orchestrator = TaskOrchestrator(
            store,
            runner,
            (_strategy(),),
            bounded_graph_execution=True,
            defer_parent_evaluation=True,
            plan_reviewer=reviewer,
            plan_reviser=reviser,
            node_assessor=_ScriptedNodeAssessor(_strategy(), routed_profile, events),
            routing_risk_floor=5,
            independent_node_assessment=True,
        )
        run = orchestrator.run(
            goal,
            _review_proposal("review-run", goal, original),
            ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=30.0),
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="review-run",
            available_capabilities=("process",),
        )
        replay = orchestrator.replay(run.id)
        inspected = inspect_graph_run(store, run.id)

        assert events == ["review-0", "revision", "review-1", "assess", "worker"], (
            events,
            tuple((item.status, item.failure_code) for item in replay.nodes),
        )
        assert reviewer.calls == [0, 1]
        assert reviser.calls == 1
        assert replay.acceptance.accepted_revision.graph == revised
        assert replay.acceptance.proposed_graph_digest == (
            replay.review_attempts[-1].proposed_graph_digest
        )
        assert len(replay.review_attempts) == 2
        assert len(replay.revision_attempts) == 1
        assert replay.review_acceptance_binding is not None
        assert replay.run.independent_node_assessment is True
        assert replay.routes[0].assessment.risk == 5
        assert replay.routes[0].assessment.required_capabilities == ("process",)
        assert inspected["plan_review"]["status"] == "revised"
        assert orchestrator.replay(run.id) == replay


def test_enabled_independent_node_assessment_fails_closed_without_assessor(
    tmp_path: Path,
) -> None:
    goal = Goal(id="assessment-goal", statement="complete one bounded task")
    graph = one_node_graph(
        goal,
        graph_id="assessment-graph",
        node_id="a",
        required_capabilities=("process",),
        max_wall_seconds=30.0,
    )
    profile = _node("a").semantic_profile
    assert profile is not None
    graph = graph.model_copy(
        update={"nodes": (graph.nodes[0].model_copy(update={"semantic_profile": profile}),)}
    )
    with SQLiteStore(tmp_path / "missing-assessor.db") as store:
        orchestrator = TaskOrchestrator(
            store,
            lambda *_args: pytest.fail("an unavailable assessment must prevent worker routing"),
            (_strategy(),),
            bounded_graph_execution=True,
            defer_parent_evaluation=True,
            independent_node_assessment=True,
        )
        with pytest.raises(ValueError, match="assessor is unavailable"):
            orchestrator.run(
                goal,
                _review_proposal("assessment-run", goal, graph),
                ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=30.0),
                harness_digest=ZERO,
                effective_policy_digest="1" * 64,
                run_id="assessment-run",
                available_capabilities=("process",),
            )


def test_advisory_review_accepts_justified_breadth_without_revision(tmp_path: Path) -> None:
    events: list[str] = []
    goal = Goal(
        id="broad-review-goal",
        statement="Exhaustively inspect every authentication path",
    )
    graph = one_node_graph(
        goal,
        graph_id="justified-broad-graph",
        node_id="a",
        required_capabilities=("process",),
        max_wall_seconds=30.0,
    )
    advisory = PlanReviewFinding(
        id="finding-advisory",
        finding_type=PlanReviewFindingType.UNCLEAR_GOAL_TRACEABILITY,
        impact=PlanReviewImpact.ADVISORY,
        affected_node_ids=("a",),
        goal_relation="The breadth is required, but traceability could be clearer.",
        smallest_correction="Clarify traceability only if the graph is edited later.",
    )
    reviewer = _ScriptedPlanReviewer(_strategy(), {0: (advisory,)}, events)
    reviser = _ScriptedPlanReviser(_strategy(), graph, events)

    with SQLiteStore(tmp_path / "advisory-review.db") as store:
        orchestrator = TaskOrchestrator(
            store,
            lambda *_args: pytest.fail("plan-only must not invoke a Worker"),
            (_strategy(),),
            plan_reviewer=reviewer,
            plan_reviser=reviser,
        )
        run = orchestrator.run(
            goal,
            _review_proposal("advisory-review-run", goal, graph),
            ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=30.0),
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="advisory-review-run",
            available_capabilities=("process",),
            plan_only=True,
        )

        replay = orchestrator.replay(run.id)
        assert events == ["review-0"]
        assert reviser.calls == 0
        assert replay.acceptance.accepted_revision.graph == graph
        assert replay.review_attempts[0].findings == (advisory,)
        assert replay.revision_attempts == ()
        assert inspect_graph_run(store, run.id)["plan_review"]["status"] == "accepted"


def test_round_one_blockers_and_review_failures_stop_before_acceptance(tmp_path: Path) -> None:
    goal = Goal(id="blocked-review-goal", statement="complete one bounded task")
    original = one_node_graph(
        goal,
        graph_id="blocked-original",
        node_id="a",
        required_capabilities=("process",),
        max_wall_seconds=30.0,
    )
    revised = original.model_copy(update={"id": "still-overengineered"})
    finding = _blocking_review_finding()
    events: list[str] = []
    reviewer = _ScriptedPlanReviewer(_strategy(), {0: (finding,), 1: (finding,)}, events)
    reviser = _ScriptedPlanReviser(_strategy(), revised, events)

    with SQLiteStore(tmp_path / "blocked-review.db") as store:
        orchestrator = TaskOrchestrator(
            store,
            lambda *_args: pytest.fail("a blocked plan must not invoke a Worker"),
            (_strategy(),),
            plan_reviewer=reviewer,
            plan_reviser=reviser,
        )
        with pytest.raises(PlanReviewGateError) as caught:
            orchestrator.run(
                goal,
                _review_proposal("blocked-review-run", goal, original),
                ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=30.0),
                harness_digest=ZERO,
                effective_policy_digest="1" * 64,
                run_id="blocked-review-run",
                available_capabilities=("process",),
            )
        assert caught.value.stable_code == "PLAN_REVIEW_BLOCKED"
        assert events == ["review-0", "revision", "review-1"]
        assert (
            len(
                store.list_records(
                    "plan_review_attempt_v2", PlanReviewAttempt, run_id="blocked-review-run"
                )
            )
            == 2
        )
        assert (
            len(
                store.list_records(
                    "plan_revision_attempt_v2",
                    PlanRevisionAttempt,
                    run_id="blocked-review-run",
                )
            )
            == 1
        )
        assert store.list_records("task_graph_acceptance_v2", TaskGraphAcceptance) == ()
        explanation = explain_any_run(store, "blocked-review-run")
        assert explanation["plan_decisions"]["plan_review"]["status"] == "blocked"
        assert explanation["final_outcome"]["failure_code"] == "PLAN_REVIEW_BLOCKED"
        assert explanation["failure_path"][-1]["stage"] == "plan_review"


@pytest.mark.parametrize(
    ("failure_kind", "stdout_artifact_digest"),
    (
        (PlanReviewFailureKind.MALFORMED_OUTPUT, "9" * 64),
        (PlanReviewFailureKind.STALE_BINDING, None),
    ),
)
def test_review_invocation_failure_is_persisted_and_fails_closed(
    tmp_path: Path,
    failure_kind: PlanReviewFailureKind,
    stdout_artifact_digest: str | None,
) -> None:
    run_id = f"failed-review-{failure_kind.value}"
    goal = Goal(id="failed-review-goal", statement="complete one bounded task")
    graph = one_node_graph(
        goal,
        graph_id="failed-review-graph",
        node_id="a",
        required_capabilities=("process",),
        max_wall_seconds=30.0,
    )
    reviewer = _ScriptedPlanReviewer(_strategy(), {0: ()}, [])

    def fail_review(*_args: object, **_kwargs: object) -> object:
        raise PlanReviewInvocationError(
            failure_kind,
            "reviewer boundary failure",
            stdout_artifact_digest=stdout_artifact_digest,
        )

    reviewer.review = fail_review  # type: ignore[method-assign]
    with SQLiteStore(tmp_path / "failed-review.db") as store:
        orchestrator = TaskOrchestrator(
            store,
            lambda *_args: pytest.fail("a failed review must not invoke a Worker"),
            (_strategy(),),
            plan_reviewer=reviewer,
            plan_reviser=_ScriptedPlanReviser(_strategy(), graph, []),
        )
        with pytest.raises(PlanReviewGateError) as caught:
            orchestrator.run(
                goal,
                _review_proposal(run_id, goal, graph),
                ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=30.0),
                harness_digest=ZERO,
                effective_policy_digest="1" * 64,
                run_id=run_id,
                available_capabilities=("process",),
            )
        attempts = store.list_records("plan_review_attempt_v2", PlanReviewAttempt, run_id=run_id)
        assert caught.value.stable_code == "PLAN_REVIEW_FAILED"
        assert len(attempts) == 1
        assert attempts[0].outcome == "failed"
        assert attempts[0].failure_code == "PLAN_REVIEW_FAILED"
        evidence = store.list_records(
            "plan_review_failure_evidence_v2",
            PlanReviewFailureEvidence,
            run_id=run_id,
        )
        assert len(evidence) == 1
        assert evidence[0].plan_review_attempt_id == attempts[0].id
        assert evidence[0].plan_review_attempt_digest == attempts[0].content_digest
        assert evidence[0].failure_kind is failure_kind
        assert evidence[0].stdout_artifact_digest == stdout_artifact_digest
        inspected = inspect_any_run(store, run_id)
        assert inspected["plan_review"]["failure_evidence"][0]["failure_kind"] == (
            failure_kind.value
        )
        assert (
            inspected["plan_review"]["failure_evidence"][0]["stdout_artifact_digest"]
            == stdout_artifact_digest
        )
        explanation = explain_any_run(store, run_id)
        assert explanation["goal"]["statement"] == goal.statement
        assert explanation["graph"]["accepted"] is False
        assert explanation["graph"]["tasks"][0]["id"] == "a"
        assert explanation["graph"]["tasks"][0]["position"] == "not_accepted"
        assert explanation["graph"]["tasks"][0]["authority"] == "proposed_only"
        assert explanation["current_state"]["ready_task_ids"] == []
        assert explanation["current_state"]["task_counts"] == {"not_accepted": 1}
        assert explanation["failure_path"][0]["stage"] == "plan_review"
        assert explanation["failure_path"][0]["reason_code"] == "PLAN_REVIEW_FAILED"
        assert explanation["final_outcome"]["failure_code"] == "PLAN_REVIEW_FAILED"
        with store.transaction() as connection:
            connection.execute(
                "DELETE FROM records WHERE kind=? AND record_id=?",
                ("pre_acceptance_goal_v2", run_id),
            )
        older_explanation = explain_any_run(store, run_id)
        assert older_explanation["goal"]["statement"] is None
        assert older_explanation["goal"]["unavailable_reason"] == (
            "goal_not_persisted_by_older_runtime"
        )
        assert store.list_records("task_graph_acceptance_v2", TaskGraphAcceptance) == ()


def test_plan_revision_failure_is_explainable_before_graph_acceptance(tmp_path: Path) -> None:
    run_id = "failed-plan-revision"
    goal = Goal(id="revision-failure-goal", statement="revise a rejected bounded plan")
    graph = one_node_graph(
        goal,
        graph_id="revision-failure-graph",
        node_id="a",
        required_capabilities=("process",),
        max_wall_seconds=30.0,
    )
    reviewer = _ScriptedPlanReviewer(_strategy(), {0: (_blocking_review_finding(),)}, [])
    reviser = _ScriptedPlanReviser(_strategy(), graph, [])

    def fail_revision(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("planner unavailable")

    reviser.revise = fail_revision  # type: ignore[method-assign]
    with SQLiteStore(tmp_path / "failed-revision.db") as store:
        orchestrator = TaskOrchestrator(
            store,
            lambda *_args: pytest.fail("failed plan revision must not invoke a Worker"),
            (_strategy(),),
            plan_reviewer=reviewer,
            plan_reviser=reviser,
        )
        with pytest.raises(PlanReviewGateError) as caught:
            orchestrator.run(
                goal,
                _review_proposal(run_id, goal, graph),
                ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=30.0),
                harness_digest=ZERO,
                effective_policy_digest="1" * 64,
                run_id=run_id,
                available_capabilities=("process",),
            )

        assert caught.value.stable_code == "GRAPH_PLANNER_FAILED"
        explanation = explain_any_run(store, run_id)
        assert explanation["goal"]["statement"] == goal.statement
        assert explanation["final_outcome"]["failure_code"] == "GRAPH_PLANNER_FAILED"
        assert explanation["failure_path"][-1]["stage"] == "plan_revision"
        assert explanation["failure_path"][-1]["reason_code"] == "GRAPH_PLANNER_FAILED"


def test_pre_acceptance_goals_are_isolated_by_run_when_goal_ids_are_reused(
    tmp_path: Path,
) -> None:
    shared_goal_id = "shared-goal-id"
    with SQLiteStore(tmp_path / "isolated-goals.db") as store:
        for suffix in ("first", "second"):
            run_id = f"goal-run-{suffix}"
            goal = Goal(id=shared_goal_id, statement=f"{suffix} distinct statement")
            graph = one_node_graph(
                goal,
                graph_id=f"graph-{suffix}",
                node_id="a",
                required_capabilities=("process",),
                max_wall_seconds=30.0,
            )
            reviewer = _ScriptedPlanReviewer(_strategy(), {0: ()}, [])

            def fail_review(*_args: object, **_kwargs: object) -> object:
                raise PlanReviewInvocationError(
                    PlanReviewFailureKind.REVIEWER_ERROR,
                    "reviewer unavailable",
                )

            reviewer.review = fail_review  # type: ignore[method-assign]
            orchestrator = TaskOrchestrator(
                store,
                lambda *_args: pytest.fail("failed plan review must not invoke a Worker"),
                (_strategy(),),
                plan_reviewer=reviewer,
                plan_reviser=_ScriptedPlanReviser(_strategy(), graph, []),
            )
            with pytest.raises(PlanReviewGateError):
                orchestrator.run(
                    goal,
                    _review_proposal(run_id, goal, graph),
                    ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=30.0),
                    harness_digest=ZERO,
                    effective_policy_digest="1" * 64,
                    run_id=run_id,
                    available_capabilities=("process",),
                )

        assert explain_any_run(store, "goal-run-first")["goal"]["statement"] == (
            "first distinct statement"
        )
        assert explain_any_run(store, "goal-run-second")["goal"]["statement"] == (
            "second distinct statement"
        )
        records = store.list_records("pre_acceptance_goal_v2", PreAcceptanceGoalRecord)
        assert {item.id for item in records} == {"goal-run-first", "goal-run-second"}


def test_tampered_review_binding_fails_replay_and_resume(tmp_path: Path) -> None:
    goal = Goal(id="tampered-review-goal", statement="complete one bounded task")
    graph = one_node_graph(
        goal,
        graph_id="tampered-review-graph",
        node_id="a",
        required_capabilities=("process",),
        max_wall_seconds=30.0,
    )
    reviewer = _ScriptedPlanReviewer(_strategy(), {0: ()}, [])
    policy = ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=30.0)
    with SQLiteStore(tmp_path / "tampered-review.db") as store:
        orchestrator = TaskOrchestrator(
            store,
            lambda *_args: pytest.fail("plan-only or rejected resume must not invoke a Worker"),
            (_strategy(),),
            plan_reviewer=reviewer,
            plan_reviser=_ScriptedPlanReviser(_strategy(), graph, []),
        )
        run = orchestrator.run(
            goal,
            _review_proposal("tampered-review-run", goal, graph),
            policy,
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="tampered-review-run",
            available_capabilities=("process",),
            plan_only=True,
        )
        binding = orchestrator.replay(run.id).review_acceptance_binding
        assert binding is not None
        store.put(
            "plan_review_acceptance_binding_v2",
            binding.model_copy(
                update={
                    "selected_proposed_graph_digest": "f" * 64,
                    "content_digest": None,
                }
            ),
            run_id=run.id,
        )
        store.put(
            "graph_run_v2",
            run.model_copy(update={"status": "paused"}),
            run_id=run.id,
            revision=run.generation + 1,
        )

        with pytest.raises(ValueError, match="acceptance binding is stale"):
            orchestrator.replay(run.id)
        with pytest.raises(ValueError, match="acceptance binding is stale"):
            orchestrator.run(
                goal,
                graph,
                policy,
                harness_digest=ZERO,
                effective_policy_digest="1" * 64,
                run_id=run.id,
                available_capabilities=("process",),
                resume=True,
            )
        assert inspect_graph_run(store, run.id)["plan_review"]["status"] == "failed"


def test_authoritative_plan_only_graph_starts_without_review(tmp_path: Path) -> None:
    goal = Goal(id="planned-start-goal", statement="start the accepted plan")
    graph = one_node_graph(
        goal,
        graph_id="planned-start-graph",
        node_id="a",
        required_capabilities=("process",),
        max_wall_seconds=30.0,
    )
    events: list[str] = []
    requests: list[WorkerRequest] = []
    reviewer = _ScriptedPlanReviewer(_strategy(), {0: ()}, events)

    def runner(
        node: Node, request: WorkerRequest, _strategy_value: ExecutionStrategy
    ) -> NodeExecutionResult:
        requests.append(request)
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id="planned-start-result",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or ZERO,
                status="succeeded",
                duration_seconds=0.01,
            ),
            criterion_evidence=(
                CriterionEvidence(
                    criterion_id=node.completion_criteria[0].id,
                    disposition="satisfied",
                    evidence_refs=(ZERO,),
                ),
            ),
        )

    policy = ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=30.0)
    with SQLiteStore(tmp_path / "planned-start.db") as store:
        orchestrator = TaskOrchestrator(
            store,
            runner,
            (_strategy(),),
            plan_reviewer=reviewer,
            plan_reviser=_ScriptedPlanReviser(_strategy(), graph, events),
        )
        planned = orchestrator.run(
            goal,
            _review_proposal("planned-start", goal, graph),
            policy,
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="planned-start",
            available_capabilities=("process",),
            plan_only=True,
        )
        started = orchestrator.run(
            goal,
            graph,
            policy,
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id=planned.id,
            available_capabilities=("process",),
            resume=True,
        )
        replay = orchestrator.replay(started.id)

    assert started.status == "completed"
    assert started.generation == 0
    assert len(requests) == 1
    assert events == ["review-0"]
    assert len(replay.review_attempts) == 1
    assert replay.controls == ()


def test_invalid_initial_graph_never_invokes_optional_reviewer(tmp_path: Path) -> None:
    events: list[str] = []
    goal = Goal(id="invalid-review-goal", statement="reject the invalid graph")
    graph = _fork_join_graph().model_copy(
        update={"budget": _fork_join_graph().budget.model_copy(update={"max_attempts": 2})}
    )
    reviewer = _ScriptedPlanReviewer(_strategy(), {0: ()}, events)
    reviser = _ScriptedPlanReviser(_strategy(), graph, events)
    with SQLiteStore(tmp_path / "invalid-review.db") as store:
        orchestrator = TaskOrchestrator(
            store,
            lambda *_args: pytest.fail("invalid graph must not invoke a Worker"),
            (_strategy(),),
            plan_reviewer=reviewer,
            plan_reviser=reviser,
        )
        with pytest.raises(GraphValidationError):
            orchestrator.run(
                goal,
                _review_proposal("invalid-review-run", goal, graph),
                ExecutionPolicy(max_nodes=3, max_attempts=3, max_wall_seconds=30.0),
                harness_digest=ZERO,
                effective_policy_digest="1" * 64,
                run_id="invalid-review-run",
                available_capabilities=("process",),
            )
        assert events == []
        assert store.list_records("plan_review_attempt_v2", PlanReviewPayload) == ()


def test_old_direct_callers_remain_not_configured_and_missing_binding_fails_replay(
    tmp_path: Path,
) -> None:
    goal = Goal(id="legacy-review-goal", statement="keep direct callers compatible")
    graph = one_node_graph(
        goal,
        graph_id="legacy-graph",
        node_id="legacy",
        max_wall_seconds=30.0,
    )
    database = tmp_path / "legacy-review.db"
    with SQLiteStore(database) as store:
        orchestrator = TaskOrchestrator(store, lambda *_args: pytest.fail(), (_strategy(),))
        run = orchestrator.run(
            goal,
            graph,
            ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=30.0),
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="legacy-review-run",
            available_capabilities=(),
            plan_only=True,
        )
        replay = orchestrator.replay(run.id)
        assert replay.review_attempts == ()
        assert replay.revision_attempts == ()
        assert replay.review_acceptance_binding is None
        assert inspect_graph_run(store, run.id)["plan_review"]["status"] == "not_configured"


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

            return (
                store.reserve_graph_node(
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
                )
                is not None
            )

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
        explanation = explain_any_run(store, cancelled.id)
        propagations = store.list_records(
            "node_control_propagation_v2",
            NodeControlPropagationRecord,
            run_id=cancelled.id,
        )
        assert store.list_records("goal_evaluator_v2", WorkerResult, run_id=cancelled.id) == ()

    assert not controller.is_alive()
    assert cancelled.status == "cancelled"
    assert cancelled.generation == 1
    assert set(calls) == {"a", "b"}
    statuses = {item.node_id: item.status for item in replay.nodes}
    assert statuses == {"a": "cancelled", "b": "cancelled", "c": "cancelled"}
    assert explanation["current_state"]["task_counts"] == {"cancelled": 3}
    assert all(
        story["execution_attempts"][-1]["authoritative_for_current_state"] is True
        for story in explanation["task_stories"]
    )
    assert any(
        story["execution_attempts"][-1]["generation_matches_run"] is False
        for story in explanation["task_stories"]
    )
    assert replay.results == ()
    assert replay.evidence == ()
    assert replay.evaluator_decisions == ()
    assert {item.node_id for item in propagations} <= {"a", "b"}
    assert len(replay.stale_results) == 2
    assert {item.result_generation for item in replay.stale_results} == {0}
    assert {item.authoritative_generation for item in replay.stale_results} == {1}
    assert [(item.action, item.generation) for item in replay.controls] == [("cancel", 1)]
    assert replay.worker_invocations == 0
    assert replay.verification_invocations == 0
    assert replay.composition_invocations == 0
    assert replay.promotion_invocations == 0


def test_parent_cancel_is_propagated_to_each_blocked_child_run(tmp_path: Path) -> None:
    database = tmp_path / "cancel-propagation.db"
    started = {name: threading.Event() for name in ("a", "b")}

    def runner(
        node: Node,
        request: WorkerRequest,
        _strategy_value: ExecutionStrategy,
    ) -> NodeExecutionResult:
        started[node.id].set()
        with SQLiteStore(database) as observer:
            for _index in range(300):
                if observer.control(request.run_id) == "cancel":
                    break
                threading.Event().wait(0.01)
            else:
                raise AssertionError("parent cancellation did not reach child run")
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id=f"cancelled-result-{node.id}",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or ZERO,
                status="cancelled",
                failure=StableFailure(
                    code=StableFailureCode.CANCELLED,
                    message="child observed parent cancellation",
                ),
                duration_seconds=0.01,
            ),
            criterion_evidence=(),
        )

    def cancel_parent() -> None:
        assert started["a"].wait(timeout=3)
        assert started["b"].wait(timeout=3)
        with SQLiteStore(database) as controller:
            controller.request_control("propagation-run", "cancel")

    controller = threading.Thread(target=cancel_parent)
    with SQLiteStore(database) as store:
        controller.start()
        run = TaskOrchestrator(store, runner, (_strategy(),), max_concurrency=2).run(
            Goal(id="propagation-goal", statement="cancel active children"),
            _fork_join_graph(),
            ExecutionPolicy(max_nodes=3, max_attempts=3, max_wall_seconds=30.0),
            harness_digest=ZERO,
            effective_policy_digest="1" * 64,
            run_id="propagation-run",
            available_capabilities=("process",),
        )
        controller.join(timeout=3)
        propagations = store.list_records(
            "node_control_propagation_v2",
            NodeControlPropagationRecord,
            run_id=run.id,
        )

    assert run.status == "cancelled", (
        run.status,
        run.failure_code,
        run.generation,
        [(item.node_id, item.cleanup_confirmed) for item in propagations],
    )
    assert not controller.is_alive()
    assert {item.node_id for item in propagations} == {"a", "b"}
    assert all(item.propagated for item in propagations)
    assert sum(item.cleanup_confirmed for item in propagations) == 2


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
            raise RuntimeError("token=supersecret transient worker boundary")
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
        diagnostics = store.list_records(
            "worker_boundary_diagnostic_v2", WorkerBoundaryDiagnostic, run_id=run.id
        )
        inspected = inspect_graph_run(store, run.id)

    assert run.status == "completed"
    assert [item.attempt for item in requests] == [0, 1]
    assert len({item.id for item in requests}) == 2
    assert len({item.content_digest for item in requests}) == 2
    assert len({item.run_id for item in requests}) == 2
    assert [(item.generation, item.attempt) for item in replay.reservations] == [(0, 0), (0, 1)]
    assert [(item.generation, item.attempt) for item in replay.routes] == [(0, 0), (0, 1)]
    assert [(item.generation, item.attempt) for item in replay.context_manifests] == [
        (0, 0),
        (0, 1),
    ]
    assert len({item.worker_request_digest for item in replay.context_manifests}) == 2
    assert all(not item.conversation_history_included for item in replay.context_manifests)
    assert all(item.assessment.semantic_profile is not None for item in replay.routes)
    assert len({item.id for item in replay.routes}) == 2
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.code == StableFailureCode.WORKER_BOUNDARY_ERROR.value
    assert diagnostic.stage == "runner"
    assert diagnostic.retryable is True
    assert diagnostic.exception_type == "RuntimeError"
    assert diagnostic.exception_message is not None
    assert "supersecret" not in diagnostic.exception_message
    assert "<redacted>" in diagnostic.exception_message
    assert diagnostic.worker_result_id is None
    assert inspected["worker_boundary_diagnostics"] == [diagnostic.model_dump(mode="json")]


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
            max_artifact_bytes=(1_000_000 if failure_kind == "budget" else 2_000_000),
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
        diagnostics = store.list_records(
            "worker_boundary_diagnostic_v2", WorkerBoundaryDiagnostic, run_id=run.id
        )

    assert run.status == "failed"
    assert len(requests) == 1
    assert len(replay.routes) == 1
    assert len(replay.reservations) == 1
    if failure_kind == "budget":
        assert run.failure_code == StableFailureCode.WORKER_BOUNDARY_ERROR.value
        assert len(diagnostics) == 1
        assert diagnostics[0].retryable is False
    else:
        assert diagnostics == ()


def test_node_execution_advance_uses_injected_transition_clock(tmp_path: Path) -> None:
    transitioned_at = NOW + timedelta(seconds=7)

    def unused_runner(
        _node: Node,
        _request: WorkerRequest,
        _strategy_value: ExecutionStrategy,
    ) -> NodeExecutionResult:
        raise AssertionError("the transition test does not invoke a worker")

    with SQLiteStore(tmp_path / "transition-clock.db") as store:
        orchestrator = TaskOrchestrator(
            store,
            unused_runner,
            (_strategy(),),
            clock=lambda: transitioned_at,
        )
        initial = NodeExecutionRecord(
            id="clocked-node",
            run_id="clocked-run",
            created_at=NOW,
            transitioned_at=NOW,
            node_id="a",
            accepted_graph_revision_digest=ZERO,
            generation=0,
            attempt=0,
            sequence=0,
            status="pending",
        )

        running = orchestrator._advance(initial, status="running")

        assert running.transitioned_at == transitioned_at
        assert running.sequence == 1
        assert store.list_records(
            "node_execution_v2", NodeExecutionRecord, run_id="clocked-run"
        ) == (running,)
