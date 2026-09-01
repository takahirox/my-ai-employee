from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from ai_employee.cli import _next_actions
from ai_employee.domain import (
    Budget,
    CompletionCriterion,
    Edge,
    EvaluationDecision,
    ExecutionPolicy,
    ExecutionStrategy,
    Goal,
    Graph,
    Node,
    NodeKind,
    OutputContract,
    RoutingMode,
)
from ai_employee.domain.v2 import (
    CriterionEvidence,
    StableFailure,
    StableFailureCode,
    WorkerBoundaryDiagnostic,
    WorkerRequest,
    WorkerResult,
)
from ai_employee.graph_evaluation import ParentCandidateEvaluationRecord
from ai_employee.inspector import inspect_graph_run
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    GraphRunRecord,
    LoopAction,
    NodeExecutionResult,
    NodeRunner,
    TaskOrchestrator,
)
from ai_employee.task_review import (
    TaskReviewBasis,
    TaskReviewConfidence,
    TaskReviewFinding,
    TaskReviewFindingType,
    TaskReviewPayload,
    TaskReviewRequest,
    TaskReviewResult,
    TaskReviewSeverity,
    bind_task_review_payload,
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


class _ScriptedTaskReviewer:
    strategy = _strategy()

    def __init__(self, findings_by_attempt: dict[int, tuple[TaskReviewFinding, ...]]) -> None:
        self.findings_by_attempt = findings_by_attempt
        self.requests: list[TaskReviewRequest] = []

    def review(self, request: TaskReviewRequest) -> TaskReviewResult:
        self.requests.append(request)
        return bind_task_review_payload(
            TaskReviewPayload(
                findings=self.findings_by_attempt.get(request.attempt, ()),
                reviewed_criterion_ids=tuple(sorted(request.criterion_ids)),
                limitations=(),
            ),
            request=request,
            record_id=f"task-review-result-{request.node_id}-{request.attempt}",
            run_id=request.run_id,
            created_at=NOW,
        )


def _review_finding(request: TaskReviewRequest, *, inferred: bool = False) -> TaskReviewFinding:
    return TaskReviewFinding(
        id="finding-correctness",
        finding_type=TaskReviewFindingType.CORRECTNESS_RISK,
        severity=TaskReviewSeverity.HIGH,
        confidence=(TaskReviewConfidence.UNCERTAIN if inferred else TaskReviewConfidence.CERTAIN),
        basis=TaskReviewBasis.INFERRED if inferred else TaskReviewBasis.OBSERVED,
        criterion_ids=tuple(sorted(request.criterion_ids)),
        description="the exact verified result still misses the semantic requirement",
        evidence_digests=tuple(sorted(request.deterministic_evidence_digests[:2])),
        artifact_digests=(),
        repair_objective="make the smallest correction needed by the criterion",
    )


class _FirstAttemptFindingReviewer(_ScriptedTaskReviewer):
    def __init__(self, *, inferred: bool = False, always: bool = False) -> None:
        super().__init__({})
        self.inferred = inferred
        self.always = always

    def review(self, request: TaskReviewRequest) -> TaskReviewResult:
        findings = (
            (_review_finding(request, inferred=self.inferred),)
            if self.always or request.attempt == 0
            else ()
        )
        self.findings_by_attempt[request.attempt] = findings
        return super().review(request)


def _run_with_review(
    store: SQLiteStore,
    graph: Graph,
    goal: Goal,
    runner: NodeRunner,
    reviewer: _ScriptedTaskReviewer,
    *,
    run_id: str,
) -> tuple[TaskOrchestrator, GraphRunRecord]:
    orchestrator = TaskOrchestrator(
        store,
        runner,
        (_strategy(),),
        task_reviewer=reviewer,
        independent_task_review=True,
    )
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


def test_malformed_patch_protocol_failure_gets_one_bound_correction_turn(
    tmp_path: Path,
) -> None:
    goal, graph, _node = _inputs(max_repairs=1)
    requests: list[WorkerRequest] = []

    def runner(
        _bound_node: Node,
        request: WorkerRequest,
        _selected: ExecutionStrategy,
    ) -> NodeExecutionResult:
        requests.append(request)
        if request.attempt:
            return _result(request, "satisfied")
        diagnostic = WorkerBoundaryDiagnostic(
            id="malformed-patch-diagnostic",
            run_id=request.run_id,
            created_at=NOW,
            adapter="scripted",
            stage="envelope",
            code="WORKER_ENVELOPE_MALFORMED",
            retryable=False,
            graph_run_id=request.graph_run_id,
            node_id=request.node_id,
            accepted_graph_revision_digest=request.accepted_graph_revision_digest,
            generation=request.generation,
            attempt=request.attempt,
            worker_request_id=request.id,
            worker_request_digest=request.content_digest,
            exception_type="PatchValidationError",
            exception_message="existing-file hunk has inconsistent line counts",
            duration_seconds=0.01,
        )
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id="malformed-worker-result",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or ZERO,
                status="failed",
                failure=StableFailure(
                    code=StableFailureCode.WORKER_PROTOCOL_ERROR,
                    message="proposal normalization failed",
                ),
                duration_seconds=0.01,
                boundary_diagnostic=diagnostic,
            ),
            criterion_evidence=(),
        )

    with SQLiteStore(tmp_path / "protocol-repair.db") as store:
        orchestrator, run = _run(store, graph, goal, runner, run_id="closed-loop-protocol-repair")
        replay = orchestrator.replay("closed-loop-protocol-repair")

    assert run.status == "completed"
    assert [request.attempt for request in requests] == [0, 1]
    assert "inconsistent line counts" in requests[1].goal
    repair = next(item for item in replay.loop_transitions if item.action is LoopAction.REPAIR)
    failed_result = next(item for item in replay.results if item.status == "failed")
    assert repair.worker_result_digest == failed_result.content_digest
    assert requests[1].accepted_feedback_digests == repair.evidence_digests


def test_failed_parent_evaluation_resumes_one_writing_node_with_exact_feedback(
    tmp_path: Path,
) -> None:
    criterion = CompletionCriterion(
        id="criterion-patch",
        description="the bounded patch is accepted",
    )
    node = Node(
        id="fix",
        kind=NodeKind.FUNCTION,
        name="fix",
        objective="fix the bounded defect",
        output_contract=OutputContract(id="contract-fix"),
        required_capabilities=("edit_intent",),
        completion_criteria=(criterion,),
    )
    graph = Graph(
        id="parent-repair-graph",
        nodes=(node,),
        entry_node_ids=(node.id,),
        terminal_node_ids=(node.id,),
        budget=Budget(
            max_attempts=2,
            max_repairs=1,
            max_loop_iterations=2,
            max_nodes=1,
            max_worker_turns=2,
            max_processes=2,
            max_wall_seconds=2.0,
            max_artifact_bytes=2_000_000,
        ),
    )
    goal = Goal(id="parent-repair-goal", statement="fix one bounded defect")
    strategy = _strategy().model_copy(update={"capabilities": ("edit_intent",)})
    requests: list[WorkerRequest] = []

    def runner(
        _bound_node: Node,
        request: WorkerRequest,
        _selected: ExecutionStrategy,
    ) -> NodeExecutionResult:
        requests.append(request)
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
                    criterion_id=criterion.id,
                    disposition="satisfied",
                    evidence_refs=(ZERO,),
                ),
            ),
        )

    with SQLiteStore(tmp_path / "parent-repair.db") as store:
        orchestrator = TaskOrchestrator(store, runner, (strategy,))
        first = orchestrator.run(
            goal,
            graph,
            ExecutionPolicy(max_nodes=1, max_attempts=2, max_wall_seconds=2.0),
            harness_digest=HARNESS,
            effective_policy_digest=POLICY,
            run_id="parent-repair",
            available_capabilities=("edit_intent",),
        )
        failed = first.model_copy(
            update={"status": "failed", "failure_code": "PARENT_VERIFICATION_FAILED"}
        )
        store.put("graph_run_v2", failed, run_id=failed.id, revision=1)
        evaluation = ParentCandidateEvaluationRecord(
            id="parent-evaluation",
            run_id=failed.id,
            created_at=NOW,
            request_digest=ZERO,
            accepted_graph_revision_digest=failed.accepted_graph_revision_digest,
            composition_record_digest=ZERO,
            composition_workspace_digest=ZERO,
            candidate_digest=ZERO,
            candidate_descriptor_digest=ZERO,
            candidate_artifact_digest=ZERO,
            effective_policy_digest=POLICY,
            goal_evaluator_digest=ZERO,
            decision=EvaluationDecision.FAIL,
            status="failed",
            failure_code="PARENT_VERIFICATION_FAILED",
        )
        store.put("parent_candidate_evaluation_v2", evaluation, run_id=failed.id)

        assert orchestrator.prepare_parent_repair(failed.id, evaluation.content_digest or ZERO)
        resumed = orchestrator.run(
            goal,
            graph,
            failed.execution_policy,
            harness_digest=HARNESS,
            effective_policy_digest=POLICY,
            run_id=failed.id,
            available_capabilities=("edit_intent",),
            resume=True,
        )
        replay = orchestrator.replay(failed.id)

    assert resumed.status == "completed"
    assert [request.attempt for request in requests] == [0, 1]
    assert "Accepted parent-candidate repair evidence" in requests[1].goal
    repair = next(item for item in replay.loop_transitions if item.action is LoopAction.REPAIR)
    assert repair.reason_code == "ACCEPTED_PARENT_EVALUATION_FEEDBACK"
    assert requests[1].accepted_feedback_digests == repair.evidence_digests


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


def test_invalid_verification_binding_fails_without_semantic_repair(tmp_path: Path) -> None:
    goal, graph, _node = _inputs(max_repairs=2)

    def runner(
        _bound_node: Node,
        request: WorkerRequest,
        _selected: ExecutionStrategy,
    ) -> NodeExecutionResult:
        return _result(request, "blocked").model_copy(
            update={"failure_code": StableFailureCode.VERIFICATION_BINDING_INVALID.value}
        )

    with SQLiteStore(tmp_path / "invalid-binding.db") as store:
        orchestrator, run = _run(store, graph, goal, runner, run_id="invalid-binding")
        replay = orchestrator.replay("invalid-binding")

    assert run.status == "failed"
    assert run.failure_code == StableFailureCode.VERIFICATION_BINDING_INVALID.value
    assert [item.action for item in replay.loop_transitions] == [LoopAction.FAIL]
    assert replay.loop_transitions[0].reason_code == (
        StableFailureCode.VERIFICATION_BINDING_INVALID.value
    )


def test_correctable_verification_with_zero_repair_budget_reports_exhaustion(
    tmp_path: Path,
) -> None:
    goal, graph, _node = _inputs(max_repairs=0)

    def runner(
        _bound_node: Node,
        request: WorkerRequest,
        _selected: ExecutionStrategy,
    ) -> NodeExecutionResult:
        return _result(request, "blocked").model_copy(
            update={"failure_code": StableFailureCode.VERIFICATION_FAILED.value}
        )

    with SQLiteStore(tmp_path / "zero-repair.db") as store:
        orchestrator, run = _run(store, graph, goal, runner, run_id="closed-loop-zero-repair")
        replay = orchestrator.replay("closed-loop-zero-repair")

    assert run.failure_code == "LOOP_ESCALATED:REPAIR_BUDGET_EXHAUSTED"
    assert replay.loop_transitions[-1].reason_code == "REPAIR_BUDGET_EXHAUSTED"
    assert _next_actions(run)


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


def test_independent_task_review_passes_after_verification_and_replays(tmp_path: Path) -> None:
    goal, graph, _node = _inputs()
    reviewer = _ScriptedTaskReviewer({0: ()})

    def runner(
        _node: Node, request: WorkerRequest, _strategy: ExecutionStrategy
    ) -> NodeExecutionResult:
        return _result(request, "satisfied")

    with SQLiteStore(tmp_path / "task-review-pass.db") as store:
        orchestrator, run = _run_with_review(
            store, graph, goal, runner, reviewer, run_id="task-review-pass"
        )
        replay = orchestrator.replay("task-review-pass")
        inspected = inspect_graph_run(store, "task-review-pass")

    assert run.status == "completed"
    assert len(reviewer.requests) == 1
    assert reviewer.requests[0].worker_result.status == "succeeded"
    assert len(replay.task_review_requests) == 1
    assert len(replay.task_review_results) == 1
    assert replay.task_review_decisions[0].action.value == "PASS"
    assert replay.loop_transitions[-1].reason_code == "TASK_REVIEW_PASS"
    assert inspected["task_reviews"]["decisions"][0]["action"] == "PASS"


def test_blocking_task_review_repairs_then_reverifies_and_reviews(tmp_path: Path) -> None:
    goal, graph, _node = _inputs(max_repairs=1)
    reviewer = _FirstAttemptFindingReviewer()
    worker_requests: list[WorkerRequest] = []

    def runner(
        _node: Node, request: WorkerRequest, _strategy: ExecutionStrategy
    ) -> NodeExecutionResult:
        worker_requests.append(request)
        return _result(request, "satisfied")

    with SQLiteStore(tmp_path / "task-review-repair.db") as store:
        orchestrator, run = _run_with_review(
            store, graph, goal, runner, reviewer, run_id="task-review-repair"
        )
        replay = orchestrator.replay("task-review-repair")
        repair = replay.loop_transitions[0]

    assert run.status == "completed"
    assert [item.action for item in replay.loop_transitions] == [
        LoopAction.REPAIR,
        LoopAction.PASS,
    ]
    assert [item.attempt for item in reviewer.requests] == [0, 1]
    assert worker_requests[1].accepted_feedback_digests == repair.evidence_digests
    assert "Accepted semantic review repair objectives:" in worker_requests[1].goal
    assert "make the smallest correction" in worker_requests[1].goal
    assert len(repair.evidence_digests) == 3
    assert [item.action.value for item in replay.task_review_decisions] == ["REPAIR", "PASS"]


def test_uncertain_task_review_escalates_without_repair(tmp_path: Path) -> None:
    goal, graph, _node = _inputs(max_repairs=1)
    reviewer = _FirstAttemptFindingReviewer(inferred=True)

    def runner(
        _node: Node, request: WorkerRequest, _strategy: ExecutionStrategy
    ) -> NodeExecutionResult:
        return _result(request, "satisfied")

    with SQLiteStore(tmp_path / "task-review-escalate.db") as store:
        orchestrator, run = _run_with_review(
            store, graph, goal, runner, reviewer, run_id="task-review-escalate"
        )
        replay = orchestrator.replay("task-review-escalate")

    assert run.status == "failed"
    assert [item.action for item in replay.loop_transitions] == [LoopAction.ESCALATE]
    assert replay.task_review_decisions[0].action.value == "ESCALATE"
    assert replay.nodes[0].failure_code == "LOOP_ESCALATED:TASK_REVIEW_OPERATOR_REQUIRED"


def test_task_review_repair_bound_exhaustion_escalates(tmp_path: Path) -> None:
    goal, graph, _node = _inputs(max_repairs=1)
    reviewer = _FirstAttemptFindingReviewer(always=True)

    def runner(
        _node: Node, request: WorkerRequest, _strategy: ExecutionStrategy
    ) -> NodeExecutionResult:
        return _result(request, "satisfied")

    with SQLiteStore(tmp_path / "task-review-exhausted.db") as store:
        orchestrator, run = _run_with_review(
            store, graph, goal, runner, reviewer, run_id="task-review-exhausted"
        )
        replay = orchestrator.replay("task-review-exhausted")

    assert run.status == "failed"
    assert [item.action for item in replay.loop_transitions] == [
        LoopAction.REPAIR,
        LoopAction.ESCALATE,
    ]
    assert replay.loop_transitions[-1].reason_code == "REPAIR_BUDGET_EXHAUSTED"


class _StaleTaskReviewer(_ScriptedTaskReviewer):
    def review(self, request: TaskReviewRequest) -> TaskReviewResult:
        valid = super().review(request)
        return valid.model_copy(update={"attempt": request.attempt + 1})


def test_stale_task_review_result_is_rejected_and_recorded(tmp_path: Path) -> None:
    goal, graph, _node = _inputs(max_repairs=1)
    reviewer = _StaleTaskReviewer({0: ()})

    def runner(
        _node: Node, request: WorkerRequest, _strategy: ExecutionStrategy
    ) -> NodeExecutionResult:
        return _result(request, "satisfied")

    with SQLiteStore(tmp_path / "task-review-stale.db") as store:
        orchestrator, run = _run_with_review(
            store, graph, goal, runner, reviewer, run_id="task-review-stale"
        )
        replay = orchestrator.replay("task-review-stale")

    assert run.status == "failed"
    assert replay.task_review_decisions[0].reason_code == "TASK_REVIEW_FAILED"
    assert len(replay.stale_task_review_results) == 1
    assert replay.loop_transitions[0].action is LoopAction.FAIL


class _LimitedTaskReviewer(_ScriptedTaskReviewer):
    def review(self, request: TaskReviewRequest) -> TaskReviewResult:
        self.requests.append(request)
        return bind_task_review_payload(
            TaskReviewPayload(
                findings=(),
                reviewed_criterion_ids=request.criterion_ids,
                limitations=("the semantic result could not be fully assessed",),
            ),
            request=request,
            record_id="limited-task-review-result",
            run_id=request.run_id,
            created_at=NOW,
        )


def test_task_review_coverage_limitation_escalates_fail_closed(tmp_path: Path) -> None:
    goal, graph, _node = _inputs(max_repairs=1)
    reviewer = _LimitedTaskReviewer({})

    def runner(
        _node: Node, request: WorkerRequest, _strategy: ExecutionStrategy
    ) -> NodeExecutionResult:
        return _result(request, "satisfied")

    with SQLiteStore(tmp_path / "task-review-limited.db") as store:
        orchestrator, run = _run_with_review(
            store, graph, goal, runner, reviewer, run_id="task-review-limited"
        )
        replay = orchestrator.replay("task-review-limited")

    assert run.status == "failed"
    assert replay.task_review_decisions[0].action.value == "ESCALATE"
    assert replay.task_review_decisions[0].reason_code == "TASK_REVIEW_COVERAGE_LIMITED"
    assert replay.loop_transitions[0].action is LoopAction.ESCALATE
    assert replay.nodes[0].failure_code == "LOOP_ESCALATED:TASK_REVIEW_COVERAGE_LIMITED"


def test_passed_task_review_is_not_reinvoked_after_pause_resume(tmp_path: Path) -> None:
    first_criterion = CompletionCriterion(id="criterion-first", description="first passed")
    second_criterion = CompletionCriterion(id="criterion-second", description="second passed")
    first = Node(
        id="first",
        kind=NodeKind.FUNCTION,
        name="first",
        objective="complete first",
        output_contract=OutputContract(id="contract-first"),
        required_capabilities=("process",),
        completion_criteria=(first_criterion,),
    )
    second = Node(
        id="second",
        kind=NodeKind.FUNCTION,
        name="second",
        objective="complete second",
        output_contract=OutputContract(id="contract-second"),
        required_capabilities=("process",),
        completion_criteria=(second_criterion,),
    )
    graph = Graph(
        id="task-review-resume-graph",
        nodes=(first, second),
        edges=(Edge(id="first-second", source_id="first", target_id="second"),),
        entry_node_ids=("first",),
        terminal_node_ids=("second",),
        budget=Budget(max_attempts=4, max_nodes=2, max_wall_seconds=4.0),
    )
    goal = Goal(id="task-review-resume-goal", statement="complete both tasks")
    database = tmp_path / "task-review-resume.db"
    reviewer = _ScriptedTaskReviewer({0: ()})
    started = Event()
    release = Event()

    def runner(
        _node: Node, request: WorkerRequest, _strategy: ExecutionStrategy
    ) -> NodeExecutionResult:
        started.set()
        assert release.wait(timeout=5)
        criterion_id = f"criterion-{request.node_id}"
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id=f"worker-result-{request.node_id}",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or ZERO,
                status="succeeded",
                duration_seconds=0.01,
            ),
            criterion_evidence=(
                CriterionEvidence(
                    criterion_id=criterion_id,
                    disposition="satisfied",
                    evidence_refs=(ZERO,),
                ),
            ),
        )

    def initial_run() -> GraphRunRecord:
        with SQLiteStore(database) as store:
            orchestrator = TaskOrchestrator(
                store,
                runner,
                (_strategy(),),
                task_reviewer=reviewer,
                independent_task_review=True,
            )
            return orchestrator.run(
                goal,
                graph,
                ExecutionPolicy(max_nodes=2, max_attempts=4, max_wall_seconds=4.0),
                harness_digest=HARNESS,
                effective_policy_digest=POLICY,
                run_id="task-review-resume",
                available_capabilities=("process",),
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(initial_run)
        assert started.wait(timeout=5)
        with SQLiteStore(database) as controller:
            controller.request_control("task-review-resume", "pause")
        release.set()
        paused = future.result(timeout=5)

    assert paused.status == "paused"
    assert [item.node_id for item in reviewer.requests] == ["first"]
    with SQLiteStore(database) as store:
        orchestrator = TaskOrchestrator(
            store,
            runner,
            (_strategy(),),
            task_reviewer=reviewer,
            independent_task_review=True,
        )
        resumed = orchestrator.run(
            goal,
            graph,
            ExecutionPolicy(max_nodes=2, max_attempts=4, max_wall_seconds=4.0),
            harness_digest=HARNESS,
            effective_policy_digest=POLICY,
            run_id="task-review-resume",
            available_capabilities=("process",),
            resume=True,
        )
        replay = orchestrator.replay("task-review-resume")

    assert resumed.status == "completed"
    assert [item.node_id for item in reviewer.requests] == ["first", "second"]
    assert len(replay.task_review_requests) == 2
    assert len(replay.task_review_results) == 2
    assert len(replay.task_review_decisions) == 2
