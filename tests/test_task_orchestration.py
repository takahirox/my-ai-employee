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
    SemanticAmbiguity,
    SemanticReasoningClass,
    SemanticScope,
    SemanticTaskProfile,
    SemanticTaskType,
)
from ai_employee.domain.models import NodeResourceBudget
from ai_employee.domain.v2 import CriterionEvidence, WorkerRequest, WorkerResult
from ai_employee.graph import GraphValidationError, accept_task_graph
from ai_employee.inspector import inspect_graph_run
from ai_employee.plan_review import (
    PlanReviewAcceptanceBinding,
    PlanReviewAttempt,
    PlanReviewFinding,
    PlanReviewFindingType,
    PlanReviewGateError,
    PlanReviewImpact,
    PlanReviewPayload,
    PlanRevisionAttempt,
    bind_plan_review,
)
from ai_employee.serialization import canonical_digest
from ai_employee.services_v2._common import identifier
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    NodeExecutionResult,
    NodeReservationRecord,
    TaskGraphAcceptance,
    TaskOrchestrator,
    one_node_graph,
)
from ai_employee.task_planning import ProposedGraph

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
        id="original-proposal",
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


def test_review_invocation_failure_is_persisted_and_fails_closed(tmp_path: Path) -> None:
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
        raise ValueError("malformed reviewer output")

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
                _review_proposal("failed-review-run", goal, graph),
                ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=30.0),
                harness_digest=ZERO,
                effective_policy_digest="1" * 64,
                run_id="failed-review-run",
                available_capabilities=("process",),
            )
        attempts = store.list_records(
            "plan_review_attempt_v2", PlanReviewAttempt, run_id="failed-review-run"
        )
        assert caught.value.stable_code == "PLAN_REVIEW_FAILED"
        assert len(attempts) == 1
        assert attempts[0].outcome == "failed"
        assert attempts[0].failure_code == "PLAN_REVIEW_FAILED"
        assert store.list_records("task_graph_acceptance_v2", TaskGraphAcceptance) == ()


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
    assert [(item.generation, item.attempt) for item in replay.context_manifests] == [
        (0, 0),
        (0, 1),
    ]
    assert len({item.worker_request_digest for item in replay.context_manifests}) == 2
    assert all(not item.conversation_history_included for item in replay.context_manifests)
    assert all(item.assessment.semantic_profile is not None for item in replay.routes)
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

    assert run.status == "failed"
    assert len(requests) == 1
    assert len(replay.routes) == 1
    assert len(replay.reservations) == 1
