"""Graph-first task orchestration with proposed-graph execution safety fences."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import ClassVar, Literal, Protocol, Self, cast

from pydantic import ConfigDict, Field, model_validator
from pydantic.main import BaseModel

from .domain import (
    AcceptedGraphRevision,
    Budget,
    CompletionCriterion,
    EvaluationDecision,
    ExecutionPolicy,
    ExecutionStrategy,
    Goal,
    Graph,
    Node,
    NodeKind,
    OutputContract,
    RoutingMode,
    TaskAssessment,
)
from .domain.base import CanonicalData, Digest, Identifier, freeze_json
from .domain.v2 import (
    ArtifactDescriptor,
    ArtifactDescriptorReference,
    CriterionEvidence,
    DigestedRecordV2,
    NonMutatingResultAcceptance,
    PredecessorOutputReference,
    WorkerRequest,
    WorkerResult,
)
from .graph import GraphValidationError, accept_task_graph, validate_task_graph
from .graph_composition import NodePatchArtifact
from .plan_review import (
    PlanReviewAcceptanceBinding,
    PlanReviewAction,
    PlanReviewAttempt,
    PlanReviewFinding,
    PlanReviewGateError,
    PlanReviewPayload,
    PlanRevisionAttempt,
    TrustedPlanReview,
    decide_plan_review_action,
    validate_plan_review,
)
from .routing import RoutingError, assess_task, select_strategy
from .serialization import canonical_digest
from .services_v2._common import identifier, now
from .storage import SQLiteStore
from .task_planning import ProposedGraph

NodeExecutionStatus = Literal[
    "pending", "routed", "running", "passed", "failed", "blocked", "cancelled"
]
GraphExecutionStatus = Literal[
    "planned", "running", "paused", "cancelled", "completed", "ready_to_promote", "failed"
]


class TaskGraphAcceptance(DigestedRecordV2):
    schema_name: ClassVar[str] = "task_graph_acceptance"
    accepted_revision: AcceptedGraphRevision
    effective_policy_digest: Digest
    harness_digest: Digest
    previous_revision_digest: Digest | None = None
    proposed_graph_digest: Digest | None = None
    replan_trigger: str | None = None
    replan_evidence: tuple[Digest, ...] = ()

    @model_validator(mode="after")
    def _revision_ancestry_is_complete(self) -> Self:
        initial = self.accepted_revision.revision_number == 1
        if initial and (
            self.previous_revision_digest is not None
            or self.replan_trigger is not None
            or self.replan_evidence
        ):
            raise ValueError("initial graph acceptance cannot carry replan ancestry")
        if not initial and (
            self.previous_revision_digest is None
            or not self.replan_trigger
            or not self.replan_evidence
        ):
            raise ValueError("replanned acceptance requires exact ancestry, trigger, and evidence")
        return self


class RetainedNodeBinding(DigestedRecordV2):
    schema_name: ClassVar[str] = "retained_node_binding"
    node_id: Identifier
    previous_revision_digest: Digest
    accepted_graph_revision_digest: Digest
    previous_generation: int = Field(ge=0)
    generation: int = Field(ge=0)
    node_contract_digest: Digest
    node_execution_digest: Digest


class NodeReservationRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "node_reservation_record"
    node_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    requested: CanonicalData
    remaining_budgets: CanonicalData


class GraphControlFact(DigestedRecordV2):
    schema_name: ClassVar[str] = "graph_control_fact"
    action: Literal["pause", "cancel", "resume"]
    generation: int = Field(ge=0)


class StaleNodeResultRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "stale_node_result_record"
    node_id: Identifier
    accepted_graph_revision_digest: Digest
    result_generation: int = Field(ge=0)
    authoritative_generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    worker_request_digest: Digest
    worker_result_digest: Digest


class NodeRouteRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "node_route_record"
    node_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    assessment: TaskAssessment
    eligible_strategy_ids: tuple[Identifier, ...] = Field(min_length=1)
    selected_strategy: ExecutionStrategy
    effective_policy_digest: Digest
    harness_digest: Digest


class NodeEvidenceRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "node_evidence_record"
    node_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    criteria: tuple[CriterionEvidence, ...]


class NodeEvaluatorRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "node_evaluator_record"
    node_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    worker_result_digest: Digest
    evidence_digest: Digest
    decision: EvaluationDecision


class GoalEvaluatorRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "goal_evaluator_record"
    goal_id: Identifier
    accepted_graph_revision_digest: Digest
    evidence_digests: tuple[Digest, ...]
    artifact_descriptor_digests: tuple[Digest, ...] = ()
    artifact_content_digests: tuple[Digest, ...] = ()
    decision: EvaluationDecision


class NodeExecutionRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "node_execution_record"
    node_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    sequence: int = Field(ge=0)
    status: NodeExecutionStatus
    route_digest: Digest | None = None
    worker_request_digest: Digest | None = None
    output_generation: int | None = Field(default=None, ge=0)
    worker_result_id: Identifier | None = None
    worker_result_digest: Digest | None = None
    evidence_id: Identifier | None = None
    evidence_digest: Digest | None = None
    evaluator_id: Identifier | None = None
    evaluator_digest: Digest | None = None
    evaluator_decision: EvaluationDecision | None = None
    failure_code: str | None = None
    workspace_id: Identifier | None = None
    workspace_digest: Digest | None = None
    work_run_id: Identifier | None = None
    patch_artifact_id: Identifier | None = None
    patch_descriptor_digest: Digest | None = None
    patch_digest: Digest | None = None
    acceptance_ledger_digest: Digest | None = None
    result_acceptance_id: Identifier | None = None
    result_acceptance_digest: Digest | None = None
    verification_result_digests: tuple[Digest, ...] = ()
    artifact_descriptors: tuple[ArtifactDescriptor, ...] = ()
    retained_from_revision_digest: Digest | None = None


class GraphRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["2"] = "2"
    id: Identifier
    goal_id: Identifier
    goal: Goal
    execution_policy: ExecutionPolicy
    accepted_graph_revision_digest: Digest
    harness_digest: Digest
    effective_policy_digest: Digest
    available_capabilities: tuple[Identifier, ...]
    execution_strategies: tuple[ExecutionStrategy, ...]
    routing_mode: RoutingMode
    fixed_strategy_id: Identifier | None = None
    allowed_strategy_ids: tuple[Identifier, ...]
    allowed_backends: tuple[str, ...]
    local_backend_allowed: bool
    status: GraphExecutionStatus
    max_concurrency: int = Field(ge=1)
    max_claims: int = Field(ge=1)
    max_replans: int = Field(default=0, ge=0)
    replan_count: int = Field(default=0, ge=0)
    max_worker_turns: int = Field(default=100, ge=1)
    max_processes: int = Field(default=100, ge=0)
    max_wall_seconds: float = Field(default=3600.0, gt=0)
    max_artifact_bytes: int = Field(default=100_000_000, ge=0)
    generation: int = Field(default=0, ge=0)
    repository: str | None = None
    base_commit: str | None = None
    operator_config_digest: Digest | None = None
    operator_config_path: str | None = None
    strategy_set: Identifier | None = None
    goal_evaluator_digest: Digest | None = None
    failure_code: str | None = None
    composition_id: Identifier | None = None
    composition_digest: Digest | None = None
    parent_candidate_artifact_id: Identifier | None = None
    parent_candidate_digest: Digest | None = None
    parent_evaluation_id: Identifier | None = None
    parent_evaluation_digest: Digest | None = None
    promotion_approval_id: Identifier | None = None
    promotion_approval_request_digest: Digest | None = None


class NodeExecutionResult(BaseModel):
    """Typed facts returned by a node worker boundary, before scheduler evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    worker_result: WorkerResult
    criterion_evidence: tuple[CriterionEvidence, ...]
    workspace_id: Identifier | None = None
    node_patch: NodePatchArtifact | None = None
    artifact_descriptors: tuple[ArtifactDescriptor, ...] = ()
    result_acceptance: NonMutatingResultAcceptance | None = None
    acceptance_ledger_digest: Digest | None = None

    @model_validator(mode="after")
    def _criterion_evidence_is_unique(self) -> Self:
        ids = tuple(item.criterion_id for item in self.criterion_evidence)
        if len(ids) != len(set(ids)):
            raise ValueError("criterion evidence must be unique per node result")
        if self.node_patch is not None and self.workspace_id != self.node_patch.workspace.id:
            raise ValueError("node patch and execution workspace must match")
        descriptor_ids = tuple(item.id for item in self.artifact_descriptors)
        if len(descriptor_ids) != len(set(descriptor_ids)):
            raise ValueError("artifact descriptors must be unique per node result")
        typed_result = self.worker_result.non_mutating_result
        acceptance = self.result_acceptance
        if (typed_result is None) != (acceptance is None):
            raise ValueError("typed result and explicit acceptance must be present together")
        if acceptance is not None:
            if (
                acceptance.worker_result_id != self.worker_result.id
                or acceptance.worker_result_digest != self.worker_result.content_digest
                or typed_result is None
                or acceptance.result_id != typed_result.id
                or acceptance.result_digest != typed_result.content_digest
            ):
                raise ValueError("typed-result acceptance is not bound to the worker result")
            if (
                acceptance.status == "accepted"
                and acceptance.artifact not in self.artifact_descriptors
            ):
                raise ValueError("accepted typed result lacks its authoritative artifact")
            if acceptance.status == "rejected" and self.criterion_evidence:
                raise ValueError("rejected typed result cannot satisfy node criteria")
        return self


class NodePatchRecord(DigestedRecordV2):
    """Replayable wrapper for a body-free node patch descriptor."""

    schema_name: ClassVar[str] = "node_patch_record"
    node_patch: NodePatchArtifact


class NodeRunner(Protocol):
    def __call__(
        self,
        node: Node,
        request: WorkerRequest,
        strategy: ExecutionStrategy,
    ) -> NodeExecutionResult: ...


class PlanReviewer(Protocol):
    strategy: ExecutionStrategy

    def review(
        self,
        goal: Goal,
        proposed_graph: ProposedGraph,
        *,
        review_round: Literal[0, 1],
        available_capabilities: tuple[str, ...],
        max_nodes: int,
        max_wall_seconds: float,
    ) -> TrustedPlanReview: ...


class PlanReviser(Protocol):
    strategy: ExecutionStrategy

    def revise(
        self,
        goal: Goal,
        original: ProposedGraph,
        blocking_findings: tuple[PlanReviewFinding, ...],
        *,
        available_capabilities: tuple[str, ...],
        max_nodes: int,
        max_wall_seconds: float,
    ) -> ProposedGraph: ...


class GraphReplay(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["2"] = "2"
    run: GraphRunRecord
    acceptance: TaskGraphAcceptance
    revision_history: tuple[TaskGraphAcceptance, ...]
    retained_node_bindings: tuple[RetainedNodeBinding, ...]
    nodes: tuple[NodeExecutionRecord, ...]
    node_history: tuple[NodeExecutionRecord, ...]
    claims: tuple[Identifier, ...]
    reservations: tuple[NodeReservationRecord, ...]
    routes: tuple[NodeRouteRecord, ...]
    results: tuple[WorkerResult, ...]
    result_acceptances: tuple[NonMutatingResultAcceptance, ...]
    evidence: tuple[NodeEvidenceRecord, ...]
    evaluator_decisions: tuple[NodeEvaluatorRecord, ...]
    controls: tuple[GraphControlFact, ...]
    stale_results: tuple[StaleNodeResultRecord, ...]
    route_count: int
    worker_result_count: int
    evidence_count: int
    evaluator_count: int
    worker_invocations: Literal[0] = 0
    verification_invocations: Literal[0] = 0
    composition_invocations: Literal[0] = 0
    promotion_invocations: Literal[0] = 0
    review_attempts: tuple[PlanReviewAttempt, ...] = ()
    revision_attempts: tuple[PlanRevisionAttempt, ...] = ()
    review_acceptance_binding: PlanReviewAcceptanceBinding | None = None


def one_node_graph(
    goal: Goal,
    *,
    graph_id: Identifier,
    node_id: Identifier,
    required_capabilities: tuple[Identifier, ...] = (),
    max_wall_seconds: float = 3600.0,
) -> Graph:
    """Represent the compatibility path as the degenerate accepted task DAG."""

    criteria = goal.completion_criteria or (
        CompletionCriterion(
            id=f"criterion-{node_id}",
            description="the node-bound worker result is accepted",
        ),
    )
    node = Node(
        id=node_id,
        kind=NodeKind.FUNCTION,
        name="Single task",
        objective=goal.statement,
        output_contract=OutputContract(id=f"contract-{node_id}"),
        required_capabilities=required_capabilities,
        completion_criteria=criteria,
    )
    return Graph(
        id=graph_id,
        nodes=(node,),
        entry_node_ids=(node.id,),
        terminal_node_ids=(node.id,),
        budget=Budget(
            max_attempts=1,
            max_nodes=1,
            max_wall_seconds=max_wall_seconds,
        ),
    )


class TaskOrchestrator:
    """Accept task DAGs and fence uncomposed planner output from execution."""

    def __init__(
        self,
        store: SQLiteStore,
        runner: NodeRunner,
        strategies: Iterable[ExecutionStrategy],
        *,
        max_concurrency: int = 2,
        routing_mode: RoutingMode = RoutingMode.ADAPTIVE,
        fixed_strategy_id: Identifier | None = None,
        allowed_strategy_ids: Iterable[str] = (),
        allowed_backends: Iterable[str] = (),
        local_backend_allowed: bool = False,
        bounded_graph_execution: bool = False,
        defer_parent_evaluation: bool = False,
        repository: str | None = None,
        base_commit: str | None = None,
        operator_config_digest: Digest | None = None,
        operator_config_path: str | None = None,
        strategy_set: Identifier | None = None,
        plan_reviewer: PlanReviewer | None = None,
        plan_reviser: PlanReviser | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.store = store
        self.runner = runner
        self.strategies = tuple(sorted(strategies, key=lambda item: item.id))
        if not self.strategies:
            raise ValueError("at least one explicitly configured strategy is required")
        self.max_concurrency = max_concurrency
        self.routing_mode = routing_mode
        self.fixed_strategy_id = fixed_strategy_id
        self.allowed_strategy_ids = tuple(allowed_strategy_ids) or tuple(
            item.id for item in self.strategies
        )
        self.allowed_backends = tuple(allowed_backends) or tuple(
            dict.fromkeys(item.backend for item in self.strategies)
        )
        self.local_backend_allowed = local_backend_allowed
        if bounded_graph_execution and not defer_parent_evaluation:
            raise ValueError("bounded graph execution must defer unavailable parent evaluation")
        self.bounded_graph_execution = bounded_graph_execution
        self.defer_parent_evaluation = defer_parent_evaluation
        self.repository = repository
        self.base_commit = base_commit
        self.operator_config_digest = operator_config_digest
        self.operator_config_path = operator_config_path
        self.strategy_set = strategy_set
        if (plan_reviewer is None) != (plan_reviser is None):
            raise ValueError("plan reviewer and revision Planner must be configured together")
        if plan_reviewer is not None and plan_reviser is not None:
            configured = {item.id: item for item in self.strategies}
            if (
                plan_reviewer.strategy != plan_reviser.strategy
                or configured.get(plan_reviewer.strategy.id) != plan_reviewer.strategy
            ):
                raise ValueError("plan-review callbacks must use one configured strategy")
        self.plan_reviewer = plan_reviewer
        self.plan_reviser = plan_reviser

    def run(
        self,
        goal: Goal,
        proposed_graph: Graph | ProposedGraph,
        policy: ExecutionPolicy,
        *,
        harness_digest: Digest,
        effective_policy_digest: Digest,
        run_id: Identifier,
        available_capabilities: Iterable[str],
        plan_only: bool = False,
        resume: bool = False,
        replan: bool = False,
    ) -> GraphRunRecord:
        if (resume or replan) and plan_only:
            raise ValueError("a resumed graph must execute")
        if resume and replan:
            raise ValueError("resume and replan are mutually exclusive")
        capabilities = tuple(available_capabilities)
        if isinstance(proposed_graph, ProposedGraph):
            proposal: ProposedGraph | None = proposed_graph
            candidate: Graph = proposed_graph.graph
        else:
            proposal = None
            candidate = proposed_graph
        configured_planners = {item.id: item for item in self.strategies}
        if proposal is not None and (
            proposal.run_id != run_id
            or proposal.goal_id != goal.id
            or proposal.goal_digest != canonical_digest(goal)
            or configured_planners.get(proposal.planner_strategy.id) != proposal.planner_strategy
            or proposal.planner_strategy.backend in {"ollama", "ollama_cli"}
            or proposal.effective_policy_digest != effective_policy_digest
            or proposal.harness_digest != harness_digest
        ):
            raise ValueError("ProposedGraph provenance does not match this run")
        if replan and proposal is None:
            raise ValueError("replan requires an already-produced strict ProposedGraph")
        previous_acceptance: TaskGraphAcceptance | None = None
        previous_revision: AcceptedGraphRevision | None = None
        if replan:
            prior_run = self.store.get("graph_run_v2", run_id, GraphRunRecord)
            acceptances = self.store.list_records(
                "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run_id
            )
            acceptances = _ordered_revision_history(acceptances)
            if not acceptances:
                raise ValueError("replan requires an accepted revision")
            _validate_revision_history(acceptances)
            previous_acceptance = acceptances[-1]
            previous_revision = previous_acceptance.accepted_revision
            if (
                prior_run.status not in {"failed", "paused"}
                or prior_run.goal != goal
                or prior_run.execution_policy != policy
                or prior_run.harness_digest != harness_digest
                or prior_run.effective_policy_digest != effective_policy_digest
                or prior_run.available_capabilities != capabilities
                or prior_run.execution_strategies != self.strategies
                or prior_run.routing_mode is not self.routing_mode
                or prior_run.fixed_strategy_id != self.fixed_strategy_id
                or prior_run.allowed_strategy_ids != self.allowed_strategy_ids
                or prior_run.allowed_backends != self.allowed_backends
                or prior_run.local_backend_allowed != self.local_backend_allowed
                or prior_run.max_concurrency != self.max_concurrency
                or prior_run.repository != self.repository
                or prior_run.base_commit != self.base_commit
                or prior_run.operator_config_digest != self.operator_config_digest
                or prior_run.operator_config_path != self.operator_config_path
                or prior_run.strategy_set != self.strategy_set
                or prior_run.replan_count >= prior_run.max_replans
                or proposal is None
                or proposal.previous_accepted_revision_digest != previous_revision.content_digest
                or not proposal.replan_trigger
                or not proposal.replan_evidence
                or len(proposal.replan_evidence) != len(set(proposal.replan_evidence))
                or candidate.budget.max_replans > prior_run.max_replans
                or candidate.budget.max_attempts > prior_run.max_claims
                or candidate.budget.max_worker_turns > prior_run.max_worker_turns
                or candidate.budget.max_processes > prior_run.max_processes
                or candidate.budget.max_wall_seconds > prior_run.max_wall_seconds
                or candidate.budget.max_artifact_bytes > prior_run.max_artifact_bytes
            ):
                raise ValueError("replan authority, ancestry, or budget is invalid")
            authoritative_evidence = _authoritative_replan_evidence(
                self.store,
                run_id,
                _required_digest(previous_revision.content_digest),
            )
            if not set(proposal.replan_evidence) <= authoritative_evidence:
                raise ValueError("replan evidence is not authoritative for the previous revision")
        final_review: PlanReviewAttempt | None = None
        review_revision: PlanRevisionAttempt | None = None
        if proposal is not None and self.plan_reviewer is not None and not resume and not replan:
            issues = validate_task_graph(candidate, policy, available_capabilities=capabilities)
            if issues:
                raise GraphValidationError(issues)
            proposal, final_review, review_revision = self._run_plan_review_gate(
                goal, proposal, policy, capabilities
            )
            candidate = proposal.graph
        validated = accept_task_graph(
            candidate,
            policy,
            previous=previous_revision,
            available_capabilities=capabilities,
        )
        if resume:
            graph_run = self.store.get("graph_run_v2", run_id, GraphRunRecord)
            acceptances = self.store.list_records(
                "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run_id
            )
            acceptances = _ordered_revision_history(acceptances)
            _validate_revision_history(acceptances)
            _load_plan_review_history(self.store, graph_run, acceptances)
            if (
                graph_run.status != "paused"
                or graph_run.goal_id != goal.id
                or graph_run.goal != goal
                or graph_run.execution_policy != policy
                or graph_run.harness_digest != harness_digest
                or graph_run.effective_policy_digest != effective_policy_digest
                or graph_run.available_capabilities != capabilities
                or graph_run.execution_strategies != self.strategies
                or graph_run.routing_mode is not self.routing_mode
                or graph_run.fixed_strategy_id != self.fixed_strategy_id
                or graph_run.allowed_strategy_ids != self.allowed_strategy_ids
                or graph_run.allowed_backends != self.allowed_backends
                or graph_run.local_backend_allowed != self.local_backend_allowed
                or graph_run.repository != self.repository
                or graph_run.base_commit != self.base_commit
                or graph_run.operator_config_digest != self.operator_config_digest
                or graph_run.operator_config_path != self.operator_config_path
                or graph_run.strategy_set != self.strategy_set
                or not acceptances
                or validated.content_digest != graph_run.accepted_graph_revision_digest
                or acceptances[-1].accepted_revision.content_digest
                != graph_run.accepted_graph_revision_digest
            ):
                raise ValueError("only the authoritative paused accepted graph can resume")
            acceptance = acceptances[-1]
            accepted = acceptance.accepted_revision
            graph_digest = _required_digest(accepted.content_digest)
            graph_run = graph_run.model_copy(
                update={
                    "status": "running",
                    "generation": graph_run.generation + 1,
                    "failure_code": None,
                }
            )
            self.store.clear_control(run_id)
            self.store.put(
                "graph_control_fact_v2",
                GraphControlFact(
                    id=identifier("graph-control"),
                    run_id=run_id,
                    created_at=now(),
                    action="resume",
                    generation=graph_run.generation,
                ),
                run_id=run_id,
            )
            self._save_run(graph_run)
        elif replan:
            assert proposal is not None and previous_acceptance is not None
            accepted = validated
            graph_digest = _required_digest(accepted.content_digest)
            self.store.put("proposed_graph_v2", proposal, run_id=run_id)
            acceptance = TaskGraphAcceptance(
                id=identifier("graph-acceptance"),
                run_id=run_id,
                created_at=now(),
                accepted_revision=accepted,
                effective_policy_digest=effective_policy_digest,
                harness_digest=harness_digest,
                previous_revision_digest=previous_acceptance.accepted_revision.content_digest,
                proposed_graph_digest=proposal.content_digest,
                replan_trigger=proposal.replan_trigger,
                replan_evidence=proposal.replan_evidence,
            )
            self.store.save_graph(run_id, accepted)
            self.store.put("task_graph_acceptance_v2", acceptance, run_id=run_id)
            graph_run = self.store.get("graph_run_v2", run_id, GraphRunRecord).model_copy(
                update={
                    "accepted_graph_revision_digest": graph_digest,
                    "status": "running",
                    "generation": self.store.get("graph_run_v2", run_id, GraphRunRecord).generation
                    + 1,
                    "replan_count": self.store.get(
                        "graph_run_v2", run_id, GraphRunRecord
                    ).replan_count
                    + 1,
                    "failure_code": None,
                    "goal_evaluator_digest": None,
                    "composition_id": None,
                    "composition_digest": None,
                    "parent_candidate_artifact_id": None,
                    "parent_candidate_digest": None,
                    "parent_evaluation_id": None,
                    "parent_evaluation_digest": None,
                    "promotion_approval_id": None,
                    "promotion_approval_request_digest": None,
                }
            )
            self.store.clear_control(run_id)
            self._save_run(graph_run)
        else:
            accepted = validated
            if proposal is not None:
                self.store.put("proposed_graph_v2", proposal, run_id=run_id)
            acceptance = TaskGraphAcceptance(
                id=identifier("graph-acceptance"),
                run_id=run_id,
                created_at=now(),
                accepted_revision=accepted,
                effective_policy_digest=effective_policy_digest,
                harness_digest=harness_digest,
                proposed_graph_digest=None if proposal is None else proposal.content_digest,
            )
            graph_digest = _required_digest(accepted.content_digest)
            self.store.save_graph(run_id, accepted)
            self.store.put("task_graph_acceptance_v2", acceptance, run_id=run_id)
            if final_review is not None:
                assert proposal is not None
                review_binding = PlanReviewAcceptanceBinding(
                    id=identifier("plan-review-acceptance"),
                    run_id=run_id,
                    created_at=now(),
                    task_graph_acceptance_digest=_required_digest(acceptance.content_digest),
                    selected_proposed_graph_digest=_required_digest(proposal.content_digest),
                    accepting_review_digest=_required_digest(final_review.content_digest),
                    revision_attempt_digest=(
                        None
                        if review_revision is None
                        else _required_digest(review_revision.content_digest)
                    ),
                )
                self.store.put("plan_review_acceptance_binding_v2", review_binding, run_id=run_id)
            max_claims = min(candidate.budget.max_attempts, policy.max_attempts)
            graph_run = GraphRunRecord(
                id=run_id,
                goal_id=goal.id,
                goal=goal,
                execution_policy=policy,
                accepted_graph_revision_digest=graph_digest,
                harness_digest=harness_digest,
                effective_policy_digest=effective_policy_digest,
                available_capabilities=capabilities,
                execution_strategies=self.strategies,
                routing_mode=self.routing_mode,
                fixed_strategy_id=self.fixed_strategy_id,
                allowed_strategy_ids=self.allowed_strategy_ids,
                allowed_backends=self.allowed_backends,
                local_backend_allowed=self.local_backend_allowed,
                status="planned" if plan_only else "running",
                max_concurrency=self.max_concurrency,
                max_claims=max_claims,
                max_replans=candidate.budget.max_replans,
                max_worker_turns=candidate.budget.max_worker_turns,
                max_processes=candidate.budget.max_processes,
                max_wall_seconds=candidate.budget.max_wall_seconds,
                max_artifact_bytes=candidate.budget.max_artifact_bytes,
                repository=self.repository,
                base_commit=self.base_commit,
                operator_config_digest=self.operator_config_digest,
                operator_config_path=self.operator_config_path,
                strategy_set=self.strategy_set,
            )
            self._save_run(graph_run)
        max_claims = graph_run.max_claims
        nodes = {node.id: node for node in accepted.graph.nodes}
        predecessors: dict[str, tuple[str, ...]] = {}
        inbound: dict[str, list[str]] = defaultdict(list)
        for edge in candidate.edges:
            inbound[edge.target_id].append(edge.source_id)
        for node_id in nodes:
            predecessors[node_id] = tuple(sorted(inbound[node_id]))

        if resume or replan:
            history = self.store.list_records(
                "node_execution_v2", NodeExecutionRecord, run_id=run_id
            )
            previous: dict[str, NodeExecutionRecord] = {}
            for item in history:
                prior = previous.get(item.node_id)
                if prior is None or (item.generation, item.attempt, item.sequence) > (
                    prior.generation,
                    prior.attempt,
                    prior.sequence,
                ):
                    previous[item.node_id] = item
            records = {}
            for node_id in sorted(nodes):
                prior = previous.get(node_id)
                previous_nodes = (
                    {}
                    if previous_revision is None
                    else {item.id: item for item in previous_revision.graph.nodes}
                )
                previous_node = previous_nodes.get(node_id)
                if (
                    replan
                    and previous_revision is not None
                    and previous_node is not None
                    and prior is not None
                    and prior.status == "passed"
                    and prior.accepted_graph_revision_digest == previous_revision.content_digest
                    and _node_contract(previous_node) == _node_contract(nodes[node_id])
                    and "edit_intent" not in nodes[node_id].required_capabilities
                    and prior.result_acceptance_id is None
                ):
                    _validate_retained_node(self.store, prior)
                    retained = prior.model_copy(
                        update={
                            "id": identifier("node-execution"),
                            "created_at": now(),
                            "accepted_graph_revision_digest": graph_digest,
                            "generation": graph_run.generation,
                            "sequence": 0,
                            "retained_from_revision_digest": (
                                prior.retained_from_revision_digest
                                or prior.accepted_graph_revision_digest
                            ),
                            "content_digest": None,
                        }
                    )
                    records[node_id] = retained
                    binding = RetainedNodeBinding(
                        id=identifier("retained-node"),
                        run_id=run_id,
                        created_at=now(),
                        node_id=node_id,
                        previous_revision_digest=prior.accepted_graph_revision_digest,
                        accepted_graph_revision_digest=graph_digest,
                        previous_generation=prior.generation,
                        generation=graph_run.generation,
                        node_contract_digest=_node_contract(nodes[node_id]),
                        node_execution_digest=_required_digest(prior.content_digest),
                    )
                    self.store.put("retained_node_binding_v2", binding, run_id=run_id)
                    continue
                if replan:
                    records[node_id] = NodeExecutionRecord(
                        id=identifier("node-execution"),
                        run_id=run_id,
                        created_at=now(),
                        node_id=node_id,
                        accepted_graph_revision_digest=graph_digest,
                        generation=graph_run.generation,
                        attempt=0,
                        sequence=0,
                        status="pending",
                    )
                    continue
                assert prior is not None
                resumable = prior.status in {"pending", "blocked"}
                records[node_id] = prior.model_copy(
                    update={
                        "id": identifier("node-execution"),
                        "created_at": now(),
                        "generation": graph_run.generation,
                        "sequence": 0,
                        "status": "pending" if resumable else prior.status,
                        "route_digest": None if resumable else prior.route_digest,
                        "worker_request_digest": (
                            None if resumable else prior.worker_request_digest
                        ),
                        "failure_code": None if resumable else prior.failure_code,
                        "content_digest": None,
                    }
                )
        else:
            records = {
                node.id: NodeExecutionRecord(
                    id=identifier("node-execution"),
                    run_id=run_id,
                    created_at=now(),
                    node_id=node.id,
                    accepted_graph_revision_digest=graph_digest,
                    generation=graph_run.generation,
                    attempt=0,
                    sequence=0,
                    status="pending",
                )
                for node in nodes.values()
            }
        for record in records.values():
            self._save_node(record)
        if plan_only:
            return graph_run
        if proposal is not None and not self.bounded_graph_execution:
            graph_run = graph_run.model_copy(
                update={
                    "status": "failed",
                    "generation": graph_run.generation + 1,
                    "failure_code": "GRAPH_EXECUTION_UNAVAILABLE",
                }
            )
            self._save_run(graph_run)
            return graph_run

        evidence_by_node: dict[str, NodeEvidenceRecord] = {}
        if resume or replan:
            for node_id, record in records.items():
                if record.status == "passed" and record.evidence_id is not None:
                    evidence_by_node[node_id] = self.store.get(
                        "node_evidence_v2", record.evidence_id, NodeEvidenceRecord
                    )
        active: dict[
            Future[NodeExecutionResult],
            tuple[str, Node, WorkerRequest, NodeReservationRecord],
        ] = {}
        stop_action: Literal["pause", "cancel"] | None = None
        limits: dict[str, int | float] = {
            "worker_turns": graph_run.max_worker_turns,
            "processes": graph_run.max_processes,
            "wall_seconds": graph_run.max_wall_seconds,
            "artifact_bytes": graph_run.max_artifact_bytes,
        }
        with ThreadPoolExecutor(
            max_workers=self.max_concurrency,
            thread_name_prefix=f"fleet-{run_id[:24]}",
        ) as pool:
            while active or any(item.status == "pending" for item in records.values()):
                if stop_action is None:
                    observed = self.store.control(run_id)
                    if observed == "pause" or observed == "cancel":
                        stop_action = cast(Literal["pause", "cancel"], observed)
                        if stop_action == "cancel":
                            graph_run = self.store.get("graph_run_v2", run_id, GraphRunRecord)
                        self.store.put(
                            "graph_control_fact_v2",
                            GraphControlFact(
                                id=identifier("graph-control"),
                                run_id=run_id,
                                created_at=now(),
                                action=stop_action,
                                generation=graph_run.generation,
                            ),
                            run_id=run_id,
                        )
                for node_id in sorted(nodes):
                    record = records[node_id]
                    if record.status != "pending":
                        continue
                    if any(
                        records[parent].status in {"failed", "blocked", "cancelled"}
                        for parent in predecessors[node_id]
                    ):
                        records[node_id] = self._advance(
                            record,
                            status="blocked",
                            failure_code="PREDECESSOR_NOT_PASS",
                        )

                if stop_action is not None and not active:
                    if stop_action == "cancel":
                        for node_id in sorted(nodes):
                            if records[node_id].status in {
                                "pending",
                                "routed",
                                "running",
                                "blocked",
                            }:
                                records[node_id] = self._advance(
                                    records[node_id],
                                    status="cancelled",
                                    failure_code="GRAPH_CANCELLED",
                                )
                    if stop_action == "pause":
                        graph_run = graph_run.model_copy(
                            update={
                                "status": "paused",
                                "failure_code": "GRAPH_PAUSED",
                            }
                        )
                        self._save_run(graph_run)
                    else:
                        graph_run = self.store.get("graph_run_v2", run_id, GraphRunRecord)
                    return graph_run
                ready = (
                    []
                    if stop_action is not None
                    else [
                        node_id
                        for node_id in sorted(nodes)
                        if records[node_id].status == "pending"
                        and all(
                            records[parent].status == "passed" for parent in predecessors[node_id]
                        )
                    ]
                )
                while ready and len(active) < self.max_concurrency:
                    node_id = ready.pop(0)
                    base_node = nodes[node_id]
                    record = records[node_id]
                    node = base_node.model_copy(
                        update={
                            "generation": graph_run.generation,
                            "attempt": record.attempt,
                        }
                    )
                    try:
                        route = self._route(
                            run_id,
                            node,
                            graph_digest,
                            effective_policy_digest,
                            harness_digest,
                        )
                    except RoutingError:
                        records[node_id] = self._advance(
                            records[node_id],
                            status="failed",
                            failure_code="NO_ELIGIBLE_STRATEGY",
                        )
                        continue
                    self.store.put("node_route_v2", route, run_id=run_id)
                    records[node_id] = self._advance(
                        records[node_id],
                        status="routed",
                        route_digest=route.content_digest,
                    )

                    def create_reservation(
                        remaining: dict[str, int | float],
                        bound_node: Node = node,
                    ) -> BaseModel:
                        return NodeReservationRecord(
                            id=identifier("node-reservation"),
                            run_id=run_id,
                            created_at=now(),
                            node_id=bound_node.id,
                            accepted_graph_revision_digest=graph_digest,
                            generation=bound_node.generation,
                            attempt=bound_node.attempt,
                            requested=freeze_json(
                                {
                                    "worker_turns": bound_node.resource_budget.worker_turns,
                                    "processes": bound_node.resource_budget.processes,
                                    "wall_seconds": bound_node.resource_budget.wall_seconds,
                                    "artifact_bytes": bound_node.resource_budget.artifact_bytes,
                                    "node_attempts": 1,
                                }
                            ),
                            remaining_budgets=freeze_json(remaining),
                        )

                    reservation = self.store.reserve_graph_node(
                        run_id,
                        node_id,
                        node.generation,
                        node.attempt,
                        max_claims=max_claims,
                        worker_turns=node.resource_budget.worker_turns,
                        processes=node.resource_budget.processes,
                        wall_seconds=node.resource_budget.wall_seconds,
                        artifact_bytes=node.resource_budget.artifact_bytes,
                        limits=limits,
                        record_factory=create_reservation,
                    )
                    if reservation is None:
                        records[node_id] = self._advance(
                            records[node_id],
                            status="failed",
                            failure_code="DUPLICATE_OR_BUDGETED_CLAIM",
                        )
                        continue
                    reservation = cast(NodeReservationRecord, reservation)
                    predecessor_outputs = self._predecessor_outputs(
                        tuple(records[parent] for parent in predecessors[node_id]),
                        graph_digest,
                        graph_run.generation,
                    )
                    prior_results = tuple(item.worker_result_digest for item in predecessor_outputs)
                    prior_artifacts = tuple(
                        artifact.artifact_digest
                        for item in predecessor_outputs
                        for artifact in item.artifact_descriptors
                    )
                    if self.bounded_graph_execution and any(
                        not item.artifact_descriptors for item in predecessor_outputs
                    ):
                        records[node_id] = self._advance(
                            records[node_id],
                            status="failed",
                            failure_code="PREDECESSOR_ARTIFACT_UNAVAILABLE",
                        )
                        continue
                    request = WorkerRequest(
                        id=identifier("worker-request"),
                        run_id=_node_worker_run_id(run_id, node),
                        created_at=now(),
                        goal=node.objective or node.name,
                        accepted_plan_digest=graph_digest,
                        node_id=node.id,
                        accepted_graph_revision_digest=graph_digest,
                        graph_run_id=run_id,
                        generation=node.generation,
                        attempt=node.attempt,
                        harness_digest=harness_digest,
                        effective_policy_digest=effective_policy_digest,
                        remaining_budgets=reservation.requested,
                        prior_result_digests=prior_results,
                        prior_artifact_digests=prior_artifacts,
                        predecessor_outputs=predecessor_outputs,
                    )
                    self.store.put("worker_request_v2", request, run_id=run_id)
                    records[node_id] = self._advance(
                        records[node_id],
                        status="running",
                        worker_request_digest=request.content_digest,
                    )
                    future = pool.submit(self.runner, node, request, route.selected_strategy)
                    active[future] = (node_id, node, request, reservation)

                if not active:
                    if any(item.status == "pending" for item in records.values()):
                        for node_id in sorted(nodes):
                            if records[node_id].status == "pending":
                                records[node_id] = self._advance(
                                    records[node_id],
                                    status="blocked",
                                    failure_code="NO_READY_NODE",
                                )
                    continue
                completed, _pending = wait(tuple(active), return_when=FIRST_COMPLETED)
                for future in sorted(completed, key=lambda item: active[item][0]):
                    node_id, node, request, reservation = active.pop(future)
                    try:
                        result = future.result()
                        self._persist_result(
                            node,
                            request,
                            result,
                            records,
                            evidence_by_node,
                            graph_digest,
                        )
                        if stop_action == "cancel":
                            records[node_id] = self._advance(
                                records[node_id],
                                status="cancelled",
                                failure_code="GRAPH_CANCELLED",
                            )
                    except Exception as error:
                        if stop_action == "cancel":
                            records[node_id] = self._advance(
                                records[node_id],
                                status="cancelled",
                                failure_code="GRAPH_CANCELLED",
                            )
                            continue
                        remaining = cast(Mapping[str, int | float], reservation.remaining_budgets)
                        retry_cap = min(
                            nodes[node_id].retry_limit,
                            accepted.graph.budget.max_retries,
                        )
                        retry_resources_available = (
                            int(remaining["node_attempts"]) > 0
                            and int(remaining["worker_turns"]) >= node.resource_budget.worker_turns
                            and int(remaining["processes"]) >= node.resource_budget.processes
                            and float(remaining["wall_seconds"])
                            >= node.resource_budget.wall_seconds
                            and int(remaining["artifact_bytes"])
                            >= node.resource_budget.artifact_bytes
                        )
                        if records[node_id].attempt < retry_cap and retry_resources_available:
                            records[node_id] = NodeExecutionRecord(
                                id=identifier("node-execution"),
                                run_id=run_id,
                                created_at=now(),
                                node_id=node_id,
                                accepted_graph_revision_digest=graph_digest,
                                generation=graph_run.generation,
                                attempt=records[node_id].attempt + 1,
                                sequence=0,
                                status="pending",
                                failure_code=f"RETRY_AFTER:{type(error).__name__}",
                            )
                            self._save_node(records[node_id])
                        else:
                            records[node_id] = self._advance(
                                records[node_id],
                                status="failed",
                                failure_code=f"WORKER_BOUNDARY:{type(error).__name__}",
                            )

        authoritative = self.store.get("graph_run_v2", run_id, GraphRunRecord)
        if authoritative.generation != graph_run.generation:
            return authoritative
        node_pass = all(item.status == "passed" for item in records.values())
        writing_graph = any("edit_intent" in node.required_capabilities for node in nodes.values())
        if self.defer_parent_evaluation and writing_graph:
            graph_run = graph_run.model_copy(
                update={
                    "status": "failed",
                    "failure_code": (
                        "PARENT_EVALUATION_UNAVAILABLE" if node_pass else "NODE_EXECUTION_FAILED"
                    ),
                }
            )
            self._save_run(graph_run)
            return graph_run

        all_evidence = tuple(evidence_by_node[node_id] for node_id in sorted(evidence_by_node))
        all_artifacts = tuple(
            artifact for record in records.values() for artifact in record.artifact_descriptors
        )
        goal_decision = (
            (
                _evaluate_goal_criteria(goal.completion_criteria, all_evidence, all_artifacts)
                if node_pass
                else EvaluationDecision.FAIL
            )
            if goal.completion_criteria
            else (
                _evaluate_criteria(
                    goal.completion_criteria,
                    tuple(item for record in all_evidence for item in record.criteria),
                )
                if node_pass
                else EvaluationDecision.FAIL
            )
        )
        goal_evaluation = GoalEvaluatorRecord(
            id=identifier("goal-evaluation"),
            run_id=run_id,
            created_at=now(),
            goal_id=goal.id,
            accepted_graph_revision_digest=graph_digest,
            evidence_digests=tuple(_required_digest(item.content_digest) for item in all_evidence),
            artifact_descriptor_digests=tuple(
                _required_digest(item.content_digest) for item in all_artifacts
            ),
            artifact_content_digests=tuple(item.artifact_digest for item in all_artifacts),
            decision=goal_decision,
        )
        self.store.put("goal_evaluator_v2", goal_evaluation, run_id=run_id)
        terminal_pass = all(
            records[node_id].status == "passed" for node_id in candidate.terminal_node_ids
        )
        completed_ok = node_pass and terminal_pass and goal_decision is EvaluationDecision.PASS
        graph_run = graph_run.model_copy(
            update={
                "status": "completed" if completed_ok else "failed",
                "goal_evaluator_digest": goal_evaluation.content_digest,
                "failure_code": None if completed_ok else "GOAL_OR_NODE_EVALUATION_FAILED",
            }
        )
        self._save_run(graph_run)
        return graph_run

    def _run_plan_review_gate(
        self,
        goal: Goal,
        proposal: ProposedGraph,
        policy: ExecutionPolicy,
        capabilities: tuple[str, ...],
    ) -> tuple[ProposedGraph, PlanReviewAttempt, PlanRevisionAttempt | None]:
        assert self.plan_reviewer is not None and self.plan_reviser is not None
        if (
            self.plan_reviewer.strategy != proposal.planner_strategy
            or self.plan_reviser.strategy != proposal.planner_strategy
        ):
            raise ValueError("review and revision must reuse the resolved Planner strategy")
        max_nodes = min(proposal.graph.budget.max_nodes, policy.max_nodes)
        max_wall_seconds = min(proposal.graph.budget.max_wall_seconds, policy.max_wall_seconds)
        self.store.put("proposed_graph_v2", proposal, run_id=proposal.run_id)
        first = self._invoke_plan_review(
            goal,
            proposal,
            review_round=0,
            capabilities=capabilities,
            max_nodes=max_nodes,
            max_wall_seconds=max_wall_seconds,
        )
        if first.action is PlanReviewAction.ACCEPT:
            return proposal, first, None

        blocking = tuple(item for item in first.findings if item.impact.value == "blocking")
        try:
            revised = self.plan_reviser.revise(
                goal,
                proposal,
                blocking,
                available_capabilities=capabilities,
                max_nodes=max_nodes,
                max_wall_seconds=max_wall_seconds,
            )
            if (
                revised.run_id != proposal.run_id
                or revised.goal_id != proposal.goal_id
                or revised.goal_digest != proposal.goal_digest
                or revised.planner_strategy != proposal.planner_strategy
                or revised.effective_policy_digest != proposal.effective_policy_digest
                or revised.harness_digest != proposal.harness_digest
                or revised.previous_accepted_revision_digest
                != proposal.previous_accepted_revision_digest
                or revised.replan_trigger != proposal.replan_trigger
                or revised.replan_evidence != proposal.replan_evidence
            ):
                raise ValueError("revised ProposedGraph provenance is stale")
            issues = validate_task_graph(revised.graph, policy, available_capabilities=capabilities)
            if issues:
                raise GraphValidationError(issues)
            revision = PlanRevisionAttempt(
                id=identifier("plan-revision"),
                run_id=proposal.run_id,
                created_at=now(),
                source_proposed_graph_digest=_required_digest(proposal.content_digest),
                triggering_review_digest=_required_digest(first.content_digest),
                planner_strategy=proposal.planner_strategy,
                status="completed",
                revised_proposed_graph_id=revised.id,
                revised_proposed_graph_digest=_required_digest(revised.content_digest),
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            revision = PlanRevisionAttempt(
                id=identifier("plan-revision"),
                run_id=proposal.run_id,
                created_at=now(),
                source_proposed_graph_digest=_required_digest(proposal.content_digest),
                triggering_review_digest=_required_digest(first.content_digest),
                planner_strategy=proposal.planner_strategy,
                status="failed",
                failure_code="GRAPH_PLANNER_FAILED",
            )
            self.store.put("plan_revision_attempt_v2", revision, run_id=proposal.run_id)
            raise PlanReviewGateError("GRAPH_PLANNER_FAILED", str(error)) from error

        self.store.put("proposed_graph_v2", revised, run_id=proposal.run_id)
        self.store.put("plan_revision_attempt_v2", revision, run_id=proposal.run_id)
        second = self._invoke_plan_review(
            goal,
            revised,
            review_round=1,
            capabilities=capabilities,
            max_nodes=max_nodes,
            max_wall_seconds=max_wall_seconds,
        )
        if second.action is PlanReviewAction.REJECT:
            raise PlanReviewGateError(
                "PLAN_REVIEW_BLOCKED", "revised ProposedGraph retains blocking findings"
            )
        return revised, second, revision

    def _invoke_plan_review(
        self,
        goal: Goal,
        proposal: ProposedGraph,
        *,
        review_round: Literal[0, 1],
        capabilities: tuple[str, ...],
        max_nodes: int,
        max_wall_seconds: float,
    ) -> PlanReviewAttempt:
        assert self.plan_reviewer is not None
        try:
            trusted = self.plan_reviewer.review(
                goal,
                proposal,
                review_round=review_round,
                available_capabilities=capabilities,
                max_nodes=max_nodes,
                max_wall_seconds=max_wall_seconds,
            )
            if not isinstance(trusted, TrustedPlanReview) or (
                trusted.run_id != proposal.run_id
                or trusted.goal_id != goal.id
                or trusted.goal_digest != canonical_digest(goal)
                or trusted.proposed_graph_id != proposal.id
                or trusted.proposed_graph_digest != proposal.content_digest
                or trusted.review_round != review_round
                or trusted.reviewer_strategy != self.plan_reviewer.strategy
                or trusted.effective_policy_digest != proposal.effective_policy_digest
                or trusted.harness_digest != proposal.harness_digest
            ):
                raise ValueError("plan review returned stale or mismatched bindings")
            attempt = _completed_plan_review_attempt(trusted)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            attempt = _failed_plan_review_attempt(
                goal, proposal, review_round, self.plan_reviewer.strategy
            )
            self.store.put("plan_review_attempt_v2", attempt, run_id=proposal.run_id)
            raise PlanReviewGateError("PLAN_REVIEW_FAILED", str(error)) from error
        self.store.put("plan_review_attempt_v2", attempt, run_id=proposal.run_id)
        return attempt

    def replay(self, run_id: Identifier) -> GraphReplay:
        """Reconstruct accepted orchestration facts without workers or services."""

        run = self.store.get("graph_run_v2", run_id, GraphRunRecord)
        acceptances = self.store.list_records(
            "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run_id
        )
        acceptances = _ordered_revision_history(acceptances)
        _validate_revision_history(acceptances)
        acceptance = acceptances[-1]
        review_attempts, revision_attempts, review_binding = _load_plan_review_history(
            self.store, run, acceptances
        )
        history = self.store.list_records("node_execution_v2", NodeExecutionRecord, run_id=run_id)
        latest: dict[str, NodeExecutionRecord] = {}
        for record in history:
            previous = latest.get(record.node_id)
            if previous is None or (record.generation, record.attempt, record.sequence) > (
                previous.generation,
                previous.attempt,
                previous.sequence,
            ):
                latest[record.node_id] = record
        routes = tuple(
            sorted(
                self.store.list_records("node_route_v2", NodeRouteRecord, run_id=run_id),
                key=lambda item: (
                    item.generation,
                    item.attempt,
                    item.node_id,
                    item.created_at,
                ),
            )
        )
        reservations = tuple(
            sorted(
                self.store.list_records(
                    "node_reservation_v2", NodeReservationRecord, run_id=run_id
                ),
                key=lambda item: (
                    item.generation,
                    item.attempt,
                    item.node_id,
                    item.created_at,
                ),
            )
        )
        evidence = {
            item.evidence_id: self.store.get(
                "node_evidence_v2", item.evidence_id, NodeEvidenceRecord
            )
            for item in history
            if item.evidence_id is not None
        }
        evaluators = {
            item.evaluator_id: self.store.get(
                "node_evaluator_v2", item.evaluator_id, NodeEvaluatorRecord
            )
            for item in history
            if item.evaluator_id is not None
        }
        results = {
            item.worker_result_id: self.store.get(
                "worker_result_v2", item.worker_result_id, WorkerResult
            )
            for item in history
            if item.worker_result_id is not None
        }
        result_acceptances = {
            item.result_acceptance_id: self.store.get(
                "non_mutating_result_acceptance_v2",
                item.result_acceptance_id,
                NonMutatingResultAcceptance,
            )
            for item in history
            if item.result_acceptance_id is not None
        }
        return GraphReplay(
            run=run,
            acceptance=acceptance,
            revision_history=acceptances,
            retained_node_bindings=tuple(
                sorted(
                    self.store.list_records(
                        "retained_node_binding_v2", RetainedNodeBinding, run_id=run_id
                    ),
                    key=lambda item: (item.generation, item.node_id),
                )
            ),
            nodes=tuple(latest[node_id] for node_id in sorted(latest)),
            node_history=history,
            claims=self.store.graph_claims(run_id),
            reservations=reservations,
            routes=routes,
            results=tuple(results.values()),
            result_acceptances=tuple(result_acceptances.values()),
            evidence=tuple(evidence.values()),
            evaluator_decisions=tuple(evaluators.values()),
            controls=tuple(
                sorted(
                    self.store.list_records(
                        "graph_control_fact_v2", GraphControlFact, run_id=run_id
                    ),
                    key=lambda item: (item.generation, item.created_at, item.id),
                )
            ),
            stale_results=tuple(
                sorted(
                    self.store.list_records(
                        "stale_node_result_v2", StaleNodeResultRecord, run_id=run_id
                    ),
                    key=lambda item: (
                        item.authoritative_generation,
                        item.node_id,
                        item.attempt,
                    ),
                )
            ),
            route_count=len(routes),
            worker_result_count=sum(
                item.worker_result_digest is not None for item in latest.values()
            ),
            evidence_count=sum(item.evidence_digest is not None for item in latest.values()),
            evaluator_count=sum(item.evaluator_digest is not None for item in latest.values()),
            review_attempts=review_attempts,
            revision_attempts=revision_attempts,
            review_acceptance_binding=review_binding,
        )

    def _route(
        self,
        run_id: str,
        node: Node,
        graph_digest: str,
        effective_policy_digest: str,
        harness_digest: str,
    ) -> NodeRouteRecord:
        assessment = assess_task(
            node.objective or node.name,
            run_id=run_id,
            risk=node.risk,
            required_capabilities=node.required_capabilities,
        ).model_copy(
            update={
                "complexity": node.complexity,
                "scale": node.scale,
                "semantic_profile": node.semantic_profile,
            }
        )
        allowed_ids = set(self.allowed_strategy_ids)
        allowed_backends = set(self.allowed_backends)
        required = set(node.required_capabilities)
        eligible = tuple(
            strategy
            for strategy in self.strategies
            if strategy.id in allowed_ids
            and strategy.backend in allowed_backends
            and (strategy.backend not in {"ollama", "ollama_cli"} or self.local_backend_allowed)
            and required <= set(strategy.capabilities)
            and assessment.risk <= strategy.max_risk
            and strategy.min_complexity <= assessment.complexity <= strategy.max_complexity
            and strategy.min_scale <= assessment.scale <= strategy.max_scale
        )
        if not eligible:
            raise RoutingError("no strategy satisfies node assessment and policy")
        selected = select_strategy(
            eligible,
            mode=self.routing_mode,
            required_capabilities=node.required_capabilities,
            strategy_capabilities={item.id: item.capabilities for item in eligible},
            fixed_strategy_id=self.fixed_strategy_id,
            assessment=assessment,
            allowed_strategy_ids=tuple(item.id for item in eligible),
            allowed_backends=tuple(dict.fromkeys(item.backend for item in eligible)),
            local_backend_allowed=self.local_backend_allowed,
        )
        return NodeRouteRecord(
            id=identifier("node-route"),
            run_id=run_id,
            created_at=now(),
            node_id=node.id,
            accepted_graph_revision_digest=graph_digest,
            generation=node.generation,
            attempt=node.attempt,
            assessment=assessment,
            eligible_strategy_ids=tuple(item.id for item in eligible),
            selected_strategy=selected,
            effective_policy_digest=effective_policy_digest,
            harness_digest=harness_digest,
        )

    def _persist_result(
        self,
        node: Node,
        request: WorkerRequest,
        result: NodeExecutionResult,
        records: dict[str, NodeExecutionRecord],
        evidence_by_node: dict[str, NodeEvidenceRecord],
        graph_digest: str,
    ) -> None:
        worker_result = result.worker_result
        patch = result.node_patch
        result_acceptance = result.result_acceptance
        authoritative = self.store.get("graph_run_v2", request.graph_run_id or "", GraphRunRecord)
        if (
            authoritative.generation != request.generation
            or authoritative.accepted_graph_revision_digest != graph_digest
        ):
            self.store.put(
                "stale_node_result_v2",
                StaleNodeResultRecord(
                    id=identifier("stale-node-result"),
                    run_id=request.graph_run_id or request.run_id,
                    created_at=now(),
                    node_id=node.id,
                    accepted_graph_revision_digest=graph_digest,
                    result_generation=request.generation,
                    authoritative_generation=authoritative.generation,
                    attempt=request.attempt,
                    worker_request_digest=_required_digest(request.content_digest),
                    worker_result_digest=_required_digest(worker_result.content_digest),
                ),
                run_id=request.graph_run_id or request.run_id,
            )
            return
        if worker_result.run_id != request.run_id:
            raise ValueError("worker result belongs to another run")
        if worker_result.request_digest != request.content_digest:
            raise ValueError("worker result is not bound to its node request")
        typed_result = worker_result.non_mutating_result
        if (typed_result is None) != (result_acceptance is None):
            raise ValueError("typed result lacks exactly one explicit acceptance")
        if result_acceptance is not None:
            if worker_result.status != "succeeded":
                raise ValueError("typed-result acceptance requires worker success")
            try:
                persisted_acceptance = self.store.get(
                    "non_mutating_result_acceptance_v2",
                    result_acceptance.id,
                    NonMutatingResultAcceptance,
                )
            except KeyError:
                raise ValueError("typed-result acceptance is absent or stale") from None
            if (
                persisted_acceptance != result_acceptance
                or result_acceptance.run_id != request.run_id
                or result_acceptance.graph_run_id != request.graph_run_id
                or result_acceptance.node_id != node.id
                or result_acceptance.accepted_graph_revision_digest != graph_digest
                or result_acceptance.generation != node.generation
                or result_acceptance.attempt != node.attempt
                or result_acceptance.worker_request_digest != request.content_digest
                or result_acceptance.worker_result_id != worker_result.id
                or result_acceptance.worker_result_digest != worker_result.content_digest
                or typed_result is None
                or result_acceptance.result_id != typed_result.id
                or result_acceptance.result_digest != typed_result.content_digest
            ):
                raise ValueError("typed-result acceptance is absent or stale")
            self.store.put(
                "non_mutating_result_acceptance_v2",
                result_acceptance,
                run_id=request.graph_run_id,
            )
            if result_acceptance.status == "rejected":
                if result_acceptance.failure_code is None:
                    raise ValueError("rejected typed result has no stable failure code")
                self.store.put("worker_result_v2", worker_result, run_id=request.graph_run_id)
                records[node.id] = self._advance(
                    records[node.id],
                    status="failed",
                    output_generation=node.generation,
                    worker_result_id=worker_result.id,
                    worker_result_digest=worker_result.content_digest,
                    result_acceptance_id=result_acceptance.id,
                    result_acceptance_digest=result_acceptance.content_digest,
                    failure_code=result_acceptance.failure_code.value,
                )
                return
            if (
                typed_result.run_id != request.run_id
                or typed_result.graph_run_id != request.graph_run_id
                or typed_result.worker_request_digest != request.content_digest
                or typed_result.node_id != node.id
                or typed_result.accepted_graph_revision_digest != graph_digest
                or typed_result.generation != node.generation
                or typed_result.attempt != node.attempt
            ):
                raise ValueError("accepted typed-result binding is stale")
            if result_acceptance.artifact not in result.artifact_descriptors:
                raise ValueError("accepted typed-result artifact is absent or stale")
        if self.bounded_graph_execution:
            if not result.artifact_descriptors:
                raise ValueError("bounded node result has no authoritative artifact descriptor")
            for descriptor in result.artifact_descriptors:
                try:
                    persisted = self.store.get(
                        "artifact_descriptor_v2", descriptor.id, ArtifactDescriptor
                    )
                except KeyError:
                    raise ValueError("node artifact descriptor is absent or stale") from None
                if (
                    persisted != descriptor
                    or descriptor.run_id != request.run_id
                    or descriptor.content_digest is None
                ):
                    raise ValueError("node artifact descriptor is absent or stale")
        self.store.put("worker_result_v2", worker_result, run_id=request.run_id)
        if patch is not None:
            self.store.put(
                "node_patch_v2",
                NodePatchRecord(
                    id=identifier("node-patch"),
                    run_id=request.graph_run_id or request.run_id,
                    created_at=now(),
                    node_patch=patch,
                ),
                run_id=request.graph_run_id,
            )
        evidence = NodeEvidenceRecord(
            id=identifier("node-evidence"),
            run_id=request.run_id,
            created_at=now(),
            node_id=node.id,
            accepted_graph_revision_digest=graph_digest,
            generation=node.generation,
            attempt=node.attempt,
            criteria=result.criterion_evidence,
        )
        self.store.put("node_evidence_v2", evidence, run_id=request.run_id)
        evidence_by_node[node.id] = evidence
        decision = (
            _evaluate_node_criteria(
                node.completion_criteria,
                result.criterion_evidence,
                result.artifact_descriptors,
            )
            if worker_result.status == "succeeded"
            else EvaluationDecision.FAIL
        )
        evaluation = NodeEvaluatorRecord(
            id=identifier("node-evaluation"),
            run_id=request.run_id,
            created_at=now(),
            node_id=node.id,
            accepted_graph_revision_digest=graph_digest,
            generation=node.generation,
            attempt=node.attempt,
            worker_result_digest=_required_digest(worker_result.content_digest),
            evidence_digest=_required_digest(evidence.content_digest),
            decision=decision,
        )
        self.store.put("node_evaluator_v2", evaluation, run_id=request.run_id)
        records[node.id] = self._advance(
            records[node.id],
            status="passed" if decision is EvaluationDecision.PASS else "failed",
            output_generation=node.generation,
            worker_result_id=worker_result.id,
            worker_result_digest=worker_result.content_digest,
            evidence_id=evidence.id,
            evidence_digest=evidence.content_digest,
            evaluator_id=evaluation.id,
            evaluator_digest=evaluation.content_digest,
            evaluator_decision=decision,
            failure_code=(
                None if decision is EvaluationDecision.PASS else "NODE_EVALUATION_NOT_PASS"
            ),
            workspace_id=result.workspace_id,
            workspace_digest=(None if patch is None else patch.workspace.content_digest),
            work_run_id=(None if patch is None else patch.workspace.run_id),
            patch_artifact_id=(None if patch is None else patch.patch.id),
            patch_descriptor_digest=(None if patch is None else patch.patch.content_digest),
            patch_digest=(None if patch is None else patch.patch.artifact_digest),
            acceptance_ledger_digest=(
                result.acceptance_ledger_digest
                if result.acceptance_ledger_digest is not None
                else (None if patch is None else patch.acceptance_ledger_digest)
            ),
            result_acceptance_id=(None if result_acceptance is None else result_acceptance.id),
            result_acceptance_digest=(
                None if result_acceptance is None else result_acceptance.content_digest
            ),
            verification_result_digests=(
                () if patch is None else patch.verification_result_digests
            ),
            artifact_descriptors=result.artifact_descriptors,
        )

    def _predecessor_outputs(
        self,
        predecessors: tuple[NodeExecutionRecord, ...],
        graph_digest: Digest,
        generation: int,
    ) -> tuple[PredecessorOutputReference, ...]:
        bindings: list[PredecessorOutputReference] = []
        for record in predecessors:
            if (
                record.status != "passed"
                or record.generation != generation
                or record.accepted_graph_revision_digest != graph_digest
                or record.output_generation is None
                or record.worker_result_id is None
                or record.worker_result_digest is None
                or record.evaluator_id is None
                or record.evaluator_digest is None
                or record.evaluator_decision is not EvaluationDecision.PASS
            ):
                raise ValueError("predecessor is not an authoritative current-generation PASS")
            worker_result = self.store.get(
                "worker_result_v2", record.worker_result_id, WorkerResult
            )
            evaluator = self.store.get(
                "node_evaluator_v2", record.evaluator_id, NodeEvaluatorRecord
            )
            if (
                worker_result.content_digest != record.worker_result_digest
                or evaluator.content_digest != record.evaluator_digest
                or evaluator.node_id != record.node_id
                or evaluator.generation != record.output_generation
                or evaluator.attempt != record.attempt
                or evaluator.accepted_graph_revision_digest
                != (record.retained_from_revision_digest or graph_digest)
                or evaluator.decision is not EvaluationDecision.PASS
            ):
                raise ValueError("predecessor result or evaluator binding is stale")
            artifacts = record.artifact_descriptors
            for artifact in artifacts:
                if (
                    self.store.get("artifact_descriptor_v2", artifact.id, ArtifactDescriptor)
                    != artifact
                ):
                    raise ValueError("predecessor artifact descriptor is stale")
            result_acceptance = (
                None
                if record.result_acceptance_id is None
                else self.store.get(
                    "non_mutating_result_acceptance_v2",
                    record.result_acceptance_id,
                    NonMutatingResultAcceptance,
                )
            )
            typed_result = worker_result.non_mutating_result
            if (typed_result is None) != (result_acceptance is None):
                raise ValueError("predecessor typed result lacks explicit acceptance")
            if result_acceptance is not None and (
                result_acceptance.content_digest != record.result_acceptance_digest
                or result_acceptance.status != "accepted"
                or result_acceptance.graph_run_id != record.run_id
                or result_acceptance.node_id != record.node_id
                or result_acceptance.accepted_graph_revision_digest
                != record.accepted_graph_revision_digest
                or result_acceptance.generation != record.output_generation
                or result_acceptance.attempt != record.attempt
                or result_acceptance.worker_request_digest != record.worker_request_digest
                or result_acceptance.worker_result_id != worker_result.id
                or result_acceptance.worker_result_digest != worker_result.content_digest
                or typed_result is None
                or typed_result.run_id != worker_result.run_id
                or typed_result.graph_run_id != record.run_id
                or typed_result.worker_request_digest != record.worker_request_digest
                or typed_result.node_id != record.node_id
                or typed_result.accepted_graph_revision_digest
                != record.accepted_graph_revision_digest
                or typed_result.generation != record.output_generation
                or typed_result.attempt != record.attempt
                or result_acceptance.result_id != typed_result.id
                or result_acceptance.result_digest != typed_result.content_digest
                or result_acceptance.artifact not in artifacts
            ):
                raise ValueError("predecessor typed-result acceptance is stale")
            references = tuple(_artifact_reference(item) for item in artifacts)
            bindings.append(
                PredecessorOutputReference(
                    node_id=record.node_id,
                    accepted_graph_revision_digest=graph_digest,
                    generation=generation,
                    result_generation=record.output_generation,
                    attempt=record.attempt,
                    worker_result_id=worker_result.id,
                    worker_result_digest=_required_digest(worker_result.content_digest),
                    artifact_descriptor_id=None if not artifacts else artifacts[0].id,
                    artifact_descriptor_digest=(
                        None if not artifacts else artifacts[0].content_digest
                    ),
                    artifact_digest=None if not artifacts else artifacts[0].artifact_digest,
                    artifact_descriptors=references,
                    evaluator_id=evaluator.id,
                    evaluator_digest=_required_digest(evaluator.content_digest),
                    result_acceptance_id=(
                        None if result_acceptance is None else result_acceptance.id
                    ),
                    result_acceptance_digest=(
                        None if result_acceptance is None else result_acceptance.content_digest
                    ),
                    non_mutating_result=typed_result,
                )
            )
        return tuple(bindings)

    def _advance(self, record: NodeExecutionRecord, **changes: object) -> NodeExecutionRecord:
        payload = record.model_dump(mode="python")
        payload.update(changes)
        payload.update({"sequence": record.sequence + 1, "content_digest": None})
        updated = NodeExecutionRecord.model_validate(payload, strict=True)
        self._save_node(updated)
        return updated

    def _save_node(self, record: NodeExecutionRecord) -> None:
        self.store.put(
            "node_execution_v2",
            record,
            run_id=record.run_id,
            revision=record.sequence + 1,
        )

    def _save_run(self, run: GraphRunRecord) -> None:
        self.store.put("graph_run_v2", run, run_id=run.id, revision=run.generation + 1)


def _evaluate_criteria(
    criteria: tuple[CompletionCriterion, ...],
    evidence: tuple[CriterionEvidence, ...],
) -> EvaluationDecision:
    by_id = {item.criterion_id: item for item in evidence}
    mandatory = tuple(item for item in criteria if item.mandatory)
    if any(item.id not in by_id for item in mandatory):
        return EvaluationDecision.FAIL
    if any(by_id[item.id].disposition != "satisfied" for item in mandatory):
        return EvaluationDecision.FAIL
    return EvaluationDecision.PASS


def _completed_plan_review_attempt(review: TrustedPlanReview) -> PlanReviewAttempt:
    return PlanReviewAttempt(
        id=identifier("plan-review-attempt"),
        run_id=review.run_id,
        created_at=now(),
        goal_id=review.goal_id,
        goal_digest=review.goal_digest,
        proposed_graph_id=review.proposed_graph_id,
        proposed_graph_digest=review.proposed_graph_digest,
        review_round=review.review_round,
        reviewer_strategy=review.reviewer_strategy,
        effective_policy_digest=review.effective_policy_digest,
        harness_digest=review.harness_digest,
        outcome="completed",
        findings=review.findings,
        action=decide_plan_review_action(review),
    )


def _failed_plan_review_attempt(
    goal: Goal,
    proposal: ProposedGraph,
    review_round: Literal[0, 1],
    strategy: ExecutionStrategy,
) -> PlanReviewAttempt:
    return PlanReviewAttempt(
        id=identifier("plan-review-attempt"),
        run_id=proposal.run_id,
        created_at=now(),
        goal_id=goal.id,
        goal_digest=canonical_digest(goal),
        proposed_graph_id=proposal.id,
        proposed_graph_digest=_required_digest(proposal.content_digest),
        review_round=review_round,
        reviewer_strategy=strategy,
        effective_policy_digest=proposal.effective_policy_digest,
        harness_digest=proposal.harness_digest,
        outcome="failed",
        action=PlanReviewAction.REJECT,
        failure_code="PLAN_REVIEW_FAILED",
    )


def _load_plan_review_history(
    store: SQLiteStore,
    run: GraphRunRecord,
    acceptances: tuple[TaskGraphAcceptance, ...],
) -> tuple[
    tuple[PlanReviewAttempt, ...],
    tuple[PlanRevisionAttempt, ...],
    PlanReviewAcceptanceBinding | None,
]:
    reviews = tuple(
        sorted(
            store.list_records("plan_review_attempt_v2", PlanReviewAttempt, run_id=run.id),
            key=lambda item: item.review_round,
        )
    )
    revisions = store.list_records("plan_revision_attempt_v2", PlanRevisionAttempt, run_id=run.id)
    bindings = store.list_records(
        "plan_review_acceptance_binding_v2",
        PlanReviewAcceptanceBinding,
        run_id=run.id,
    )
    if not reviews and not revisions and not bindings:
        return (), (), None
    if not acceptances or len(bindings) != 1 or len(revisions) > 1:
        raise ValueError("plan-review acceptance history is missing or ambiguous")
    if len(reviews) not in {1, 2} or any(item.outcome != "completed" for item in reviews):
        raise ValueError("plan-review attempt history is incomplete")
    if tuple(item.review_round for item in reviews) != tuple(range(len(reviews))):
        raise ValueError("plan-review rounds are missing, duplicated, or out of order")

    initial = acceptances[0]
    binding = bindings[0]
    proposals = store.list_records("proposed_graph_v2", ProposedGraph, run_id=run.id)
    by_digest = {_required_digest(item.content_digest): item for item in proposals}
    selected_digest = initial.proposed_graph_digest
    if selected_digest is None or selected_digest not in by_digest:
        raise ValueError("reviewed graph acceptance has no exact persisted proposal")
    expected_goal_digest = canonical_digest(run.goal)
    for review in reviews:
        proposal = by_digest.get(review.proposed_graph_digest)
        if (
            review.run_id != run.id
            or review.goal_id != run.goal_id
            or review.goal_digest != expected_goal_digest
            or review.effective_policy_digest != run.effective_policy_digest
            or review.harness_digest != run.harness_digest
            or review.reviewer_strategy not in run.execution_strategies
            or proposal is None
            or proposal.id != review.proposed_graph_id
            or proposal.run_id != run.id
            or proposal.goal_id != run.goal_id
            or proposal.goal_digest != expected_goal_digest
            or proposal.effective_policy_digest != run.effective_policy_digest
            or proposal.harness_digest != run.harness_digest
        ):
            raise ValueError("plan-review evidence has stale run bindings")
        if validate_plan_review(
            PlanReviewPayload(findings=review.findings),
            goal=run.goal,
            proposed_graph=proposal,
        ):
            raise ValueError("plan-review findings have stale graph bindings")

    final = reviews[-1]
    revision_digest: Digest | None = None
    if revisions:
        revision = revisions[0]
        if (
            revision.run_id != run.id
            or len(reviews) != 2
            or reviews[0].action is not PlanReviewAction.REQUEST_REVISION
            or final.action is not PlanReviewAction.ACCEPT
            or revision.status != "completed"
            or revision.source_proposed_graph_digest != reviews[0].proposed_graph_digest
            or revision.triggering_review_digest != reviews[0].content_digest
            or revision.revised_proposed_graph_digest != final.proposed_graph_digest
            or revision.planner_strategy != reviews[0].reviewer_strategy
        ):
            raise ValueError("plan-review revision chain is invalid")
        revision_digest = _required_digest(revision.content_digest)
    elif len(reviews) != 1 or final.action is not PlanReviewAction.ACCEPT:
        raise ValueError("plan-review acceptance has an invalid round chain")

    if (
        selected_digest != final.proposed_graph_digest
        or binding.run_id != run.id
        or binding.task_graph_acceptance_digest != initial.content_digest
        or binding.selected_proposed_graph_digest != selected_digest
        or binding.accepting_review_digest != final.content_digest
        or binding.revision_attempt_digest != revision_digest
        or by_digest[selected_digest].graph != initial.accepted_revision.graph
    ):
        raise ValueError("plan-review acceptance binding is stale or mismatched")
    return reviews, revisions, binding


def _ordered_revision_history(
    acceptances: tuple[TaskGraphAcceptance, ...],
) -> tuple[TaskGraphAcceptance, ...]:
    return tuple(
        sorted(
            acceptances,
            key=lambda item: item.accepted_revision.revision_number,
        )
    )


def _validate_revision_history(acceptances: tuple[TaskGraphAcceptance, ...]) -> None:
    expected = tuple(range(1, len(acceptances) + 1))
    actual = tuple(item.accepted_revision.revision_number for item in acceptances)
    if actual != expected:
        raise ValueError("accepted revision history is missing, duplicated, or unordered")
    for index, acceptance in enumerate(acceptances):
        if index == 0:
            if acceptance.previous_revision_digest is not None:
                raise ValueError("initial accepted revision has an ancestor")
            continue
        previous = acceptances[index - 1]
        if (
            acceptance.previous_revision_digest != previous.accepted_revision.content_digest
            or acceptance.previous_revision_digest == acceptance.accepted_revision.content_digest
        ):
            raise ValueError("accepted revision ancestry is missing or cyclic")


def _authoritative_replan_evidence(
    store: SQLiteStore,
    run_id: Identifier,
    previous_revision_digest: Digest,
) -> set[Digest]:
    authoritative: set[Digest] = set()
    records = store.list_records("node_execution_v2", NodeExecutionRecord, run_id=run_id)
    for record in records:
        if record.accepted_graph_revision_digest != previous_revision_digest:
            continue
        if record.content_digest is not None:
            authoritative.add(record.content_digest)
        if record.evidence_id is not None and record.evidence_digest is not None:
            evidence = store.get("node_evidence_v2", record.evidence_id, NodeEvidenceRecord)
            if (
                evidence.content_digest == record.evidence_digest
                and evidence.accepted_graph_revision_digest
                in {
                    previous_revision_digest,
                    record.retained_from_revision_digest,
                }
            ):
                authoritative.add(record.evidence_digest)
        if record.evaluator_id is not None and record.evaluator_digest is not None:
            evaluator = store.get("node_evaluator_v2", record.evaluator_id, NodeEvaluatorRecord)
            if (
                evaluator.content_digest == record.evaluator_digest
                and evaluator.accepted_graph_revision_digest
                in {
                    previous_revision_digest,
                    record.retained_from_revision_digest,
                }
            ):
                authoritative.add(record.evaluator_digest)
    from .graph_evaluation import ParentCandidateEvaluationRecord

    for evaluation in store.list_records(
        "parent_candidate_evaluation_v2",
        ParentCandidateEvaluationRecord,
        run_id=run_id,
    ):
        if (
            evaluation.accepted_graph_revision_digest == previous_revision_digest
            and evaluation.content_digest is not None
        ):
            authoritative.add(evaluation.content_digest)
    return authoritative


def _validate_retained_node(
    store: SQLiteStore,
    record: NodeExecutionRecord,
) -> None:
    if (
        record.content_digest is None
        or record.worker_result_id is None
        or record.worker_result_digest is None
        or record.evidence_id is None
        or record.evidence_digest is None
        or record.evaluator_id is None
        or record.evaluator_digest is None
        or record.evaluator_decision is not EvaluationDecision.PASS
    ):
        raise ValueError("retained PASS node has an incomplete immutable contract")
    worker_result = store.get("worker_result_v2", record.worker_result_id, WorkerResult)
    evidence = store.get("node_evidence_v2", record.evidence_id, NodeEvidenceRecord)
    evaluator = store.get("node_evaluator_v2", record.evaluator_id, NodeEvaluatorRecord)
    evidence_revision = (
        record.retained_from_revision_digest or record.accepted_graph_revision_digest
    )
    if (
        worker_result.content_digest != record.worker_result_digest
        or evidence.content_digest != record.evidence_digest
        or evidence.node_id != record.node_id
        or evidence.accepted_graph_revision_digest != evidence_revision
        or evaluator.content_digest != record.evaluator_digest
        or evaluator.node_id != record.node_id
        or evaluator.accepted_graph_revision_digest != evidence_revision
        or evaluator.worker_result_digest != record.worker_result_digest
        or evaluator.evidence_digest != record.evidence_digest
        or evaluator.decision is not EvaluationDecision.PASS
    ):
        raise ValueError("retained PASS node contract is stale or tampered")
    descriptor_ids = tuple(item.id for item in record.artifact_descriptors)
    if len(descriptor_ids) != len(set(descriptor_ids)):
        raise ValueError("retained PASS node has duplicate artifact descriptors")
    for descriptor in record.artifact_descriptors:
        if (
            descriptor.content_digest is None
            or store.get("artifact_descriptor_v2", descriptor.id, ArtifactDescriptor) != descriptor
        ):
            raise ValueError("retained PASS node artifact is stale or tampered")
    if record.result_acceptance_id is not None:
        acceptance = store.get(
            "non_mutating_result_acceptance_v2",
            record.result_acceptance_id,
            NonMutatingResultAcceptance,
        )
        typed_result = worker_result.non_mutating_result
        if (
            acceptance.content_digest != record.result_acceptance_digest
            or acceptance.status != "accepted"
            or typed_result is None
            or acceptance.result_id != typed_result.id
            or acceptance.result_digest != typed_result.content_digest
            or acceptance.artifact not in record.artifact_descriptors
        ):
            raise ValueError("retained PASS typed-result acceptance is stale or tampered")
    elif worker_result.non_mutating_result is not None:
        raise ValueError("retained PASS typed result lacks explicit acceptance")


def _required_digest(value: str | None) -> str:
    if value is None:
        raise ValueError("persisted digested record is missing its digest")
    return value


def _node_worker_run_id(graph_run_id: str, node: Node) -> str:
    digest = canonical_digest(
        {
            "graph_run_id": graph_run_id,
            "node_id": node.id,
            "generation": node.generation,
            "attempt": node.attempt,
        }
    )
    return f"node-{digest[:32]}"


def _node_contract(node: Node) -> str:
    return canonical_digest(
        {
            "objective": node.objective,
            "required_capabilities": node.required_capabilities,
            "output_contract": node.output_contract,
            "completion_criteria": node.completion_criteria,
            "resource_budget": node.resource_budget,
            "risk": node.risk,
            "complexity": node.complexity,
            "scale": node.scale,
            "retry_limit": node.retry_limit,
            "max_iterations": node.max_iterations,
        }
    )


def _artifact_reference(descriptor: ArtifactDescriptor) -> ArtifactDescriptorReference:
    return ArtifactDescriptorReference(
        descriptor_id=descriptor.id,
        descriptor_digest=_required_digest(descriptor.content_digest),
        artifact_digest=descriptor.artifact_digest,
        logical_kind=descriptor.logical_kind,
        media_type=descriptor.media_type,
        size_bytes=descriptor.size_bytes,
        producer_action_id=descriptor.producer_action_id,
    )


def _evaluate_node_criteria(
    criteria: tuple[CompletionCriterion, ...],
    evidence: tuple[CriterionEvidence, ...],
    artifacts: tuple[ArtifactDescriptor, ...],
) -> EvaluationDecision:
    decision = _evaluate_criteria(criteria, evidence)
    if decision is not EvaluationDecision.PASS:
        return decision
    evidence_by_id = {item.criterion_id: item for item in evidence}
    artifacts_by_id = {item.id: item for item in artifacts}
    artifacts_by_kind: dict[str, list[ArtifactDescriptor]] = defaultdict(list)
    for artifact in artifacts:
        artifacts_by_kind[artifact.logical_kind].append(artifact)
    for criterion in criteria:
        if not criterion.mandatory:
            continue
        covered = evidence_by_id[criterion.id]
        for required in criterion.required_artifact_ids:
            matching: tuple[ArtifactDescriptor, ...]
            if required in artifacts_by_id:
                matching = (artifacts_by_id[required],)
            else:
                matching = tuple(artifacts_by_kind[required])
            if len(matching) != 1:
                return EvaluationDecision.FAIL
            descriptor = matching[0]
            required_refs = {
                _required_digest(descriptor.content_digest),
                descriptor.artifact_digest,
            }
            if not required_refs <= set(covered.evidence_refs):
                return EvaluationDecision.FAIL
    return EvaluationDecision.PASS


def _evaluate_goal_criteria(
    criteria: tuple[CompletionCriterion, ...],
    evidence: tuple[NodeEvidenceRecord, ...],
    artifacts: tuple[ArtifactDescriptor, ...],
) -> EvaluationDecision:
    node_evidence = {item.criterion_id: item for record in evidence for item in record.criteria}
    by_id = {item.id: item for item in artifacts}
    by_kind: dict[str, list[ArtifactDescriptor]] = defaultdict(list)
    for artifact in artifacts:
        by_kind[artifact.logical_kind].append(artifact)
    for criterion in criteria:
        if not criterion.mandatory:
            continue
        if criterion.required_artifact_ids:
            for required in criterion.required_artifact_ids:
                if required in by_id:
                    continue
                if len(by_kind[required]) != 1:
                    return EvaluationDecision.FAIL
            continue
        covered = node_evidence.get(criterion.id)
        if covered is None or covered.disposition != "satisfied":
            return EvaluationDecision.FAIL
    return EvaluationDecision.PASS
