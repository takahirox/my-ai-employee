"""Graph-first task orchestration with proposed-graph execution safety fences."""

from __future__ import annotations

import re
import signal
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import suppress
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, ClassVar, Literal, Protocol, Self, cast

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
    GoalTaskKind,
    Graph,
    Node,
    NodeKind,
    NodeResourceBudget,
    OutputContract,
    RoutingMode,
    SemanticReasoningClass,
    SemanticScope,
    SemanticTaskProfile,
    TaskAssessment,
)
from .domain.base import (
    CanonicalData,
    Digest,
    Identifier,
    SchemaModel,
    StableStrEnum,
    UtcTimestamp,
    ensure_utc,
    freeze_json,
)
from .domain.v2 import (
    ArtifactDescriptor,
    ArtifactDescriptorReference,
    CriterionEvidence,
    DigestedRecordV2,
    ExecutionResult,
    NonMutatingResultAcceptance,
    PredecessorOutputReference,
    StableFailureCode,
    WorkerBoundaryDiagnostic,
    WorkerContextManifest,
    WorkerRequest,
    WorkerResult,
)
from .graph import GraphValidationError, accept_task_graph, validate_task_graph
from .graph_composition import NodePatchArtifact
from .plan_review import (
    PlanReviewAcceptanceBinding,
    PlanReviewAction,
    PlanReviewAttempt,
    PlanReviewFailureEvidence,
    PlanReviewFailureKind,
    PlanReviewFinding,
    PlanReviewGateError,
    PlanReviewInvocationError,
    PlanReviewPayload,
    PlanRevisionAttempt,
    TrustedPlanReview,
    decide_plan_review_action,
    validate_plan_review,
)
from .routing import (
    RoutingError,
    assess_task,
    merge_semantic_profile,
    profile_compatibility_bands,
    select_strategy,
)
from .run_ownership import (
    OwnerFenceViolationRecord,
    RunExecutionOwnerRecord,
    RunLeaseClosureRecord,
    RunLeaseHeartbeatRecord,
    RunOwnerConflictRecord,
)
from .serialization import canonical_digest
from .services_v2._common import identifier, now
from .storage import SQLiteStore
from .task_planning import PlannerRoutingDecision, ProposedGraph
from .task_review import (
    StaleTaskReviewResult,
    TaskReviewAction,
    TaskReviewDecision,
    TaskReviewRequest,
    TaskReviewResult,
    TaskReviewSeverity,
    decide_task_review,
    validate_task_review_result,
)
from .worker_supervision import (
    TimeoutRecoveryRecord,
    WorkerAttemptObservation,
    WorkerAttemptSupervisor,
    WorkerBudgetPreflightRecord,
    WorkerSupervisionPolicy,
    WorkerTimeoutProfileRecord,
    WorkerTimeoutRule,
    inadequate_authorities,
    select_node_timeout,
    timeout_recovery_action,
)

NodeExecutionStatus = Literal[
    "pending", "routed", "running", "passed", "failed", "blocked", "cancelled"
]
GraphExecutionStatus = Literal[
    "planned",
    "running",
    "paused",
    "cancelled",
    "completed",
    "ready_to_promote",
    "failed",
    "interrupted",
]

_OWNED_TERMINAL_GRAPH_STATES = frozenset(
    {"paused", "cancelled", "completed", "failed", "interrupted"}
)


_PROTOCOL_PREFLIGHT_CORRECTION_REASON = "ACCEPTED_PROTOCOL_PREFLIGHT_CORRECTION"
_PROTOCOL_PREFLIGHT_CORRECTION_FAILURE_CODES = frozenset(
    {
        StableFailureCode.PATCH_PREFLIGHT_FAILED.value,
        StableFailureCode.WORKER_PROTOCOL_ERROR.value,
        StableFailureCode.WORKER_EMPTY_OUTPUT.value,
        StableFailureCode.WORKER_STRUCTURED_OUTPUT_MISSING.value,
    }
)


class RunOwnershipLost(RuntimeError):
    """The caller no longer holds authority for the graph execution."""


class _RunTerminationSignal(BaseException):
    """Turn SIGTERM into bounded authoritative interruption handling."""


class _ContainmentThreadPoolExecutor(ThreadPoolExecutor):
    """Do not let an uncooperative runner defeat the scheduler cleanup bound."""

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.shutdown(wait=False, cancel_futures=True)


class LoopAction(StableStrEnum):
    """Deterministic authoritative outcomes for one bounded orchestration step."""

    PASS = "PASS"
    RETRY = "RETRY"
    REPAIR = "REPAIR"
    REPLAN = "REPLAN"
    ESCALATE = "ESCALATE"
    FAIL = "FAIL"


class LoopTransitionRecord(DigestedRecordV2):
    """Replayable reason and exact bindings for a closed-loop transition."""

    schema_name: ClassVar[str] = "loop_transition_record"
    action: LoopAction
    reason_code: str = Field(min_length=1, max_length=200)
    accepted_graph_revision_digest: Digest
    next_graph_revision_digest: Digest | None = None
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    node_id: Identifier | None = None
    worker_request_digest: Digest | None = None
    worker_result_digest: Digest | None = None
    evidence_digests: tuple[Digest, ...] = ()
    consumed: int = Field(ge=0)
    limit: int = Field(ge=0)

    @model_validator(mode="after")
    def _bindings_match_action(self) -> Self:
        if self.action is LoopAction.REPLAN:
            if (
                self.node_id is not None
                or not self.evidence_digests
                or self.next_graph_revision_digest is None
                or self.next_graph_revision_digest == self.accepted_graph_revision_digest
            ):
                raise ValueError("REPLAN requires source, target, graph evidence, and no node")
        elif self.action is LoopAction.ESCALATE and self.node_id is None:
            if not self.evidence_digests or self.next_graph_revision_digest is not None:
                raise ValueError("graph ESCALATE requires evidence and no target revision")
        elif self.node_id is None:
            raise ValueError("node loop transition requires a node binding")
        elif self.next_graph_revision_digest is not None:
            raise ValueError("node loop transition cannot carry a next graph revision")
        if self.action in {LoopAction.PASS, LoopAction.REPAIR} and (
            self.worker_request_digest is None
            or self.worker_result_digest is None
            or len(self.evidence_digests) < 2
        ):
            raise ValueError("evaluated node transition requires request, result, and evidence")
        if len(self.evidence_digests) != len(set(self.evidence_digests)):
            raise ValueError("loop transition evidence must be unique")
        if self.consumed > self.limit and self.action is not LoopAction.ESCALATE:
            raise ValueError("only ESCALATE may describe an exhausted bound")
        return self


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


class PreAcceptanceGoalRecord(DigestedRecordV2):
    """Run-scoped Goal retained when plan review can fail before GraphRun creation."""

    schema_name: ClassVar[str] = "pre_acceptance_goal_record"
    goal: Goal
    goal_digest: Digest

    @model_validator(mode="after")
    def _goal_binding_is_exact(self) -> Self:
        if self.id != self.run_id or self.goal_digest != canonical_digest(self.goal):
            raise ValueError("pre-acceptance Goal identity or digest is stale")
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


class PlannerNodeRoutingHints(SchemaModel):
    """Planner-authored routing-shaped values retained only as provenance."""

    complexity: int = Field(ge=1, le=10)
    scale: int = Field(ge=1, le=10)
    risk: int = Field(ge=0, le=10)
    required_capabilities: tuple[Identifier, ...] = ()
    semantic_profile: SemanticTaskProfile | None = None


class NodeRoutingFacts(SchemaModel):
    """Deterministic facts which semantic classification cannot weaken."""

    risk: int = Field(ge=0, le=10)
    required_capabilities: tuple[Identifier, ...] = ()
    dependency_ids: tuple[Identifier, ...] = ()
    completion_criterion_ids: tuple[Identifier, ...] = ()
    context_character_count: int = Field(ge=0, le=10_000)
    effective_policy_digest: Digest
    harness_digest: Digest


class NodeSemanticAssessmentRecord(DigestedRecordV2):
    """Strict independently produced profile bound to one accepted node."""

    schema_name: ClassVar[str] = "node_semantic_assessment_record"
    node_id: Identifier
    accepted_graph_revision_digest: Digest
    node_subject_digest: Digest
    planner_hints: PlannerNodeRoutingHints
    routing_facts: NodeRoutingFacts
    assessment_strategy: ExecutionStrategy
    semantic_profile: SemanticTaskProfile
    assessment: TaskAssessment

    @model_validator(mode="after")
    def _assessment_matches_deterministic_floors(self) -> Self:
        complexity, scale = profile_compatibility_bands(self.semantic_profile)
        if (
            self.assessment.run_id != self.run_id
            or self.assessment.semantic_profile != self.semantic_profile
            or self.assessment.complexity != complexity
            or self.assessment.scale != scale
            or self.assessment.risk != self.routing_facts.risk
            or self.assessment.required_capabilities != self.routing_facts.required_capabilities
            or self.assessment.context_character_count != self.routing_facts.context_character_count
        ):
            raise ValueError("semantic node assessment weakens or mismatches routing floors")
        return self


class NodeRouteRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "node_route_record"
    node_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    assessment: TaskAssessment
    planner_hints: PlannerNodeRoutingHints | None = None
    routing_facts: NodeRoutingFacts | None = None
    semantic_assessment_digest: Digest | None = None
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


class TaskResultReviewer(Protocol):
    strategy: ExecutionStrategy

    def review(self, request: TaskReviewRequest) -> TaskReviewResult: ...


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
    transitioned_at: UtcTimestamp
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


class WorkerTimeoutAuthorityRecord(DigestedRecordV2):
    """Exact timeout sources bound to one parent node and child worker run."""

    schema_name: ClassVar[str] = "worker_timeout_authority_record"
    graph_run_id: Identifier
    node_id: Identifier
    child_run_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    timeout_profile_digest: Digest | None = None
    operator_config_digest: Digest | None = None
    rule_version: str | None = None
    rule_id: Identifier | None = None
    recommended_timeout_seconds: float | None = Field(default=None, gt=0)
    profile_minimum_seconds: float | None = Field(default=None, gt=0)
    adapter_timeout_seconds: float = Field(gt=0)
    node_attempt_timeout_seconds: float = Field(gt=0)
    policy_timeout_seconds: float = Field(gt=0)
    remaining_run_timeout_seconds: float | None = Field(default=None, ge=0)
    effective_timeout_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _effective_timeout_is_the_strict_minimum(self) -> Self:
        ceilings = [
            self.adapter_timeout_seconds,
            self.node_attempt_timeout_seconds,
            self.policy_timeout_seconds,
        ]
        if self.remaining_run_timeout_seconds is not None:
            ceilings.append(self.remaining_run_timeout_seconds)
        if self.effective_timeout_seconds != min(ceilings):
            raise ValueError("effective worker timeout must equal every authority ceiling minimum")
        profile_fields = (
            self.timeout_profile_digest,
            self.operator_config_digest,
            self.rule_version,
            self.rule_id,
            self.recommended_timeout_seconds,
            self.profile_minimum_seconds,
            self.remaining_run_timeout_seconds,
        )
        if any(item is not None for item in profile_fields) and any(
            item is None for item in profile_fields
        ):
            raise ValueError("profile-bound timeout authority fields must be complete")
        return self


class NodeWatchdogRecord(DigestedRecordV2):
    """Scheduler containment fact for one expired accepted node attempt."""

    schema_name: ClassVar[str] = "node_watchdog_record"
    graph_run_id: Identifier
    node_id: Identifier
    child_run_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    allowance_seconds: float = Field(gt=0)
    cleanup_grace_seconds: float = Field(ge=0)
    outcome: Literal["signal_sent", "cleanup_confirmed", "cleanup_failed"]
    timeout_profile_digest: Digest | None = None


class NodeControlPropagationRecord(DigestedRecordV2):
    """Parent-to-child cancellation and cleanup acknowledgement."""

    schema_name: ClassVar[str] = "node_control_propagation_record"
    graph_run_id: Identifier
    node_id: Identifier
    child_run_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    action: Literal["cancel"] = "cancel"
    propagated: bool
    cleanup_confirmed: bool


class DiagnosticPersistenceFailureRecord(DigestedRecordV2):
    """Minimal fail-safe fact when rich boundary diagnosis cannot be constructed."""

    schema_name: ClassVar[str] = "diagnostic_persistence_failure_record"
    graph_run_id: Identifier
    node_id: Identifier
    child_run_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    original_exception_type: str
    diagnostic_exception_type: str
    effective_timeout_seconds: float = Field(ge=0)


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
    independent_node_assessment: bool = False
    independent_task_review: bool = False
    task_reviewer_strategy: ExecutionStrategy | None = None
    task_review_block_severities: tuple[TaskReviewSeverity, ...] = (
        TaskReviewSeverity.CRITICAL,
        TaskReviewSeverity.HIGH,
    )
    planner_routing: PlannerRoutingDecision | None = None
    status: GraphExecutionStatus
    max_concurrency: int = Field(ge=1)
    max_claims: int = Field(ge=1)
    max_retries: int = Field(default=0, ge=0)
    max_replans: int = Field(default=0, ge=0)
    replan_count: int = Field(default=0, ge=0)
    max_repairs: int = Field(default=0, ge=0)
    max_worker_turns: int = Field(default=100, ge=1)
    max_processes: int = Field(default=100, ge=0)
    max_wall_seconds: float = Field(default=3600.0, gt=0)
    max_artifact_bytes: int = Field(default=100_000_000, ge=0)
    generation: int = Field(default=0, ge=0)
    execution_attempt: int = Field(default=0, ge=0)
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
    failure_code: str | None = None

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


class NodeAssessor(Protocol):
    strategy: ExecutionStrategy

    def assess(
        self,
        goal: str,
        deterministic: TaskAssessment,
    ) -> SemanticTaskProfile: ...


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
    context_manifests: tuple[WorkerContextManifest, ...]
    semantic_assessments: tuple[NodeSemanticAssessmentRecord, ...] = ()
    results: tuple[WorkerResult, ...]
    result_acceptances: tuple[NonMutatingResultAcceptance, ...]
    evidence: tuple[NodeEvidenceRecord, ...]
    evaluator_decisions: tuple[NodeEvaluatorRecord, ...]
    controls: tuple[GraphControlFact, ...]
    stale_results: tuple[StaleNodeResultRecord, ...]
    loop_transitions: tuple[LoopTransitionRecord, ...] = ()
    task_review_requests: tuple[TaskReviewRequest, ...] = ()
    task_review_results: tuple[TaskReviewResult, ...] = ()
    task_review_decisions: tuple[TaskReviewDecision, ...] = ()
    stale_task_review_results: tuple[StaleTaskReviewResult, ...] = ()
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

    writing = goal.task_kind is GoalTaskKind.MUTATING and "edit_intent" in required_capabilities
    verification_processes = max(
        1,
        len(
            {
                requirement
                for criterion in goal.completion_criteria
                for requirement in criterion.verification_requirement_ids
            }
        ),
    )
    criteria = goal.completion_criteria or (
        CompletionCriterion(
            id=f"criterion-{node_id}",
            source=(
                "accepted_non_mutating_result"
                if goal.task_kind is GoalTaskKind.NON_MUTATING
                else "custom"
            ),
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
        resource_budget=NodeResourceBudget(
            processes=verification_processes if writing else 1,
            wall_seconds=max_wall_seconds / 2 if writing else max_wall_seconds,
        ),
    )
    budget = (
        Budget(
            max_attempts=2,
            max_repairs=1,
            max_loop_iterations=2,
            max_nodes=1,
            max_wall_seconds=max_wall_seconds,
            max_worker_turns=2,
            max_processes=verification_processes * 2,
            max_artifact_bytes=2_000_000,
        )
        if writing
        else Budget(max_attempts=1, max_nodes=1, max_wall_seconds=max_wall_seconds)
    )
    return Graph(
        id=graph_id,
        nodes=(node,),
        entry_node_ids=(node.id,),
        terminal_node_ids=(node.id,),
        budget=budget,
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
        node_assessor: NodeAssessor | None = None,
        routing_risk_floor: int = 0,
        independent_node_assessment: bool = False,
        task_reviewer: TaskResultReviewer | None = None,
        independent_task_review: bool = False,
        task_review_block_severities: Iterable[TaskReviewSeverity] = (
            TaskReviewSeverity.CRITICAL,
            TaskReviewSeverity.HIGH,
        ),
        clock: Callable[[], datetime] = now,
        owner_instance_id: Identifier | None = None,
        lease_duration_seconds: float = 15.0,
        heartbeat_interval_seconds: float = 5.0,
        worker_supervision_policy: WorkerSupervisionPolicy | None = None,
        adapter_timeout_seconds: float | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.store = store
        self.runner = runner
        self.clock = clock
        if lease_duration_seconds <= 0:
            raise ValueError("run lease duration must be positive")
        if heartbeat_interval_seconds <= 0 or heartbeat_interval_seconds >= lease_duration_seconds:
            raise ValueError("run heartbeat interval must be positive and shorter than its lease")
        self.owner_instance_id = owner_instance_id or identifier("fleet-owner")
        self.lease_duration_seconds = lease_duration_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._run_owner: RunExecutionOwnerRecord | None = None
        self._next_heartbeat_at: datetime | None = None
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
        if not 0 <= routing_risk_floor <= 10:
            raise ValueError("routing risk floor must be between 0 and 10")
        self.routing_risk_floor = routing_risk_floor
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
        if node_assessor is not None:
            configured = {item.id: item for item in self.strategies}
            if configured.get(node_assessor.strategy.id) != node_assessor.strategy or (
                node_assessor.strategy.backend in {"ollama", "ollama_cli"}
                and not self.local_backend_allowed
            ):
                raise ValueError("node assessor must use an authorized configured strategy")
        self.node_assessor = node_assessor
        self.independent_node_assessment = independent_node_assessment
        if independent_task_review != (task_reviewer is not None):
            raise ValueError("independent task review requires exactly one configured reviewer")
        if task_reviewer is not None:
            configured = {item.id: item for item in self.strategies}
            if configured.get(task_reviewer.strategy.id) != task_reviewer.strategy or (
                task_reviewer.strategy.backend in {"ollama", "ollama_cli"}
                and not self.local_backend_allowed
            ):
                raise ValueError("task reviewer must use an authorized configured strategy")
        block_severities = tuple(task_review_block_severities)
        if not block_severities or len(block_severities) != len(set(block_severities)):
            raise ValueError("task-review blocking severities must be non-empty and unique")
        self.task_reviewer = task_reviewer
        self.independent_task_review = independent_task_review
        self.task_review_block_severities = block_severities
        self.worker_supervision_policy = worker_supervision_policy or WorkerSupervisionPolicy(
            rules=(
                WorkerTimeoutRule(
                    id="compatibility",
                    recommended_timeout_seconds=600.0,
                    minimum_timeout_seconds=0.001,
                ),
            )
        )
        if adapter_timeout_seconds is not None and adapter_timeout_seconds <= 0:
            raise ValueError("adapter timeout must be positive")
        self.adapter_timeout_seconds = adapter_timeout_seconds

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
        """Execute with bounded signal/error terminalization once ownership is acquired."""

        self._run_owner = None
        self._next_heartbeat_at = None
        previous_sigterm = None
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.getsignal(signal.SIGTERM)

            def interrupt_for_sigterm(_signum: int, _frame: object) -> None:
                raise _RunTerminationSignal("SIGTERM")

            signal.signal(signal.SIGTERM, interrupt_for_sigterm)
        try:
            return self._run_impl(
                goal,
                proposed_graph,
                policy,
                harness_digest=harness_digest,
                effective_policy_digest=effective_policy_digest,
                run_id=run_id,
                available_capabilities=available_capabilities,
                plan_only=plan_only,
                resume=resume,
                replan=replan,
            )
        except BaseException as error:
            if self._run_owner is not None:
                status: GraphExecutionStatus = (
                    "interrupted"
                    if isinstance(error, (KeyboardInterrupt, _RunTerminationSignal))
                    else "failed"
                )
                self._terminalize_after_exception(status, type(error).__name__)
            raise
        finally:
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)
            self._run_owner = None
            self._next_heartbeat_at = None

    def _run_impl(
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
            or (
                proposal.planner_strategy.backend in {"ollama", "ollama_cli"}
                and not self.local_backend_allowed
            )
            or proposal.effective_policy_digest != effective_policy_digest
            or proposal.harness_digest != harness_digest
        ):
            raise ValueError("ProposedGraph provenance does not match this run")
        if proposal is not None and proposal.planner_routing is not None:
            self._validate_planner_routing(goal, proposal, capabilities)
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
            if prior_run.replan_count >= prior_run.max_replans:
                authoritative_evidence = _authoritative_replan_evidence(
                    self.store,
                    run_id,
                    _required_digest(previous_revision.content_digest),
                )
                if (
                    proposal is not None
                    and proposal.replan_evidence
                    and len(proposal.replan_evidence) == len(set(proposal.replan_evidence))
                    and set(proposal.replan_evidence) <= authoritative_evidence
                ):
                    self._save_loop_transition(
                        LoopTransitionRecord(
                            id=identifier("loop-transition"),
                            run_id=run_id,
                            created_at=now(),
                            action=LoopAction.ESCALATE,
                            reason_code="REPLAN_BUDGET_EXHAUSTED",
                            accepted_graph_revision_digest=_required_digest(
                                previous_revision.content_digest
                            ),
                            generation=prior_run.generation,
                            attempt=0,
                            evidence_digests=proposal.replan_evidence,
                            consumed=prior_run.replan_count,
                            limit=prior_run.max_replans,
                        )
                    )
                raise ValueError("replan authority, ancestry, or budget is invalid")
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
                or prior_run.independent_node_assessment != self.independent_node_assessment
                or prior_run.independent_task_review != self.independent_task_review
                or prior_run.task_reviewer_strategy
                != (None if self.task_reviewer is None else self.task_reviewer.strategy)
                or prior_run.task_review_block_severities != self.task_review_block_severities
                or prior_run.max_concurrency != self.max_concurrency
                or prior_run.repository != self.repository
                or prior_run.base_commit != self.base_commit
                or prior_run.operator_config_digest != self.operator_config_digest
                or prior_run.operator_config_path != self.operator_config_path
                or prior_run.strategy_set != self.strategy_set
                or proposal is None
                or proposal.planner_routing != prior_run.planner_routing
                or proposal.previous_accepted_revision_digest != previous_revision.content_digest
                or not proposal.replan_trigger
                or not proposal.replan_evidence
                or len(proposal.replan_evidence) != len(set(proposal.replan_evidence))
                or candidate.budget.max_replans > prior_run.max_replans
                or candidate.budget.max_retries > prior_run.max_retries
                or candidate.budget.max_repairs > prior_run.max_repairs
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
                graph_run.status not in {"planned", "paused"}
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
                or graph_run.independent_node_assessment != self.independent_node_assessment
                or graph_run.independent_task_review != self.independent_task_review
                or graph_run.task_reviewer_strategy
                != (None if self.task_reviewer is None else self.task_reviewer.strategy)
                or graph_run.task_review_block_severities != self.task_review_block_severities
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
                raise ValueError("only an authoritative planned or paused graph can start")
            acceptance = acceptances[-1]
            accepted = acceptance.accepted_revision
            graph_digest = _required_digest(accepted.content_digest)
            starting_planned = graph_run.status == "planned"
            graph_run = graph_run.model_copy(
                update={
                    "status": "running",
                    "generation": (
                        graph_run.generation if starting_planned else graph_run.generation + 1
                    ),
                    "execution_attempt": graph_run.execution_attempt + 1,
                    "failure_code": None,
                }
            )
            if not starting_planned:
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
            self._acquire_run_owner(graph_run)
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
                    "execution_attempt": self.store.get(
                        "graph_run_v2", run_id, GraphRunRecord
                    ).execution_attempt
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
            self._acquire_run_owner(graph_run)
            self._save_run(graph_run)
            self._save_loop_transition(
                LoopTransitionRecord(
                    id=identifier("loop-transition"),
                    run_id=run_id,
                    created_at=now(),
                    action=LoopAction.REPLAN,
                    reason_code="ACCEPTED_AUTHORITATIVE_REPLAN",
                    accepted_graph_revision_digest=_required_digest(
                        previous_acceptance.accepted_revision.content_digest
                    ),
                    next_graph_revision_digest=graph_digest,
                    generation=graph_run.generation,
                    attempt=0,
                    evidence_digests=proposal.replan_evidence,
                    consumed=graph_run.replan_count,
                    limit=graph_run.max_replans,
                )
            )
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
                independent_node_assessment=self.independent_node_assessment,
                independent_task_review=self.independent_task_review,
                task_reviewer_strategy=(
                    None if self.task_reviewer is None else self.task_reviewer.strategy
                ),
                task_review_block_severities=self.task_review_block_severities,
                planner_routing=None if proposal is None else proposal.planner_routing,
                status="planned" if plan_only else "running",
                max_concurrency=self.max_concurrency,
                max_claims=max_claims,
                max_retries=candidate.budget.max_retries,
                max_replans=candidate.budget.max_replans,
                max_repairs=candidate.budget.max_repairs,
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
            if not plan_only:
                self._acquire_run_owner(graph_run)
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
                    and not self.independent_task_review
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
                            "transitioned_at": self.clock(),
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
                        transitioned_at=self.clock(),
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
                        "transitioned_at": self.clock(),
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
                    transitioned_at=self.clock(),
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
        independent_node_assessment = (
            self.independent_node_assessment
            and self.routing_mode is RoutingMode.ADAPTIVE
            and acceptance.proposed_graph_digest is not None
        )
        if independent_node_assessment:
            self._prepare_node_assessments(
                run_id,
                {
                    node_id: node
                    for node_id, node in nodes.items()
                    if records[node_id].status == "pending"
                },
                predecessors,
                graph_digest,
                effective_policy_digest,
                harness_digest,
                invoke=not resume,
            )
        if plan_only:
            return graph_run
        if proposal is not None and not self.bounded_graph_execution:
            graph_run = graph_run.model_copy(
                update={
                    "status": "failed",
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
        loop_counts: dict[tuple[str, LoopAction], int] = defaultdict(int)
        correction_counts: dict[str, int] = defaultdict(int)
        for transition in self.store.list_records(
            "loop_transition_v2", LoopTransitionRecord, run_id=run_id
        ):
            if transition.node_id is not None:
                if (
                    transition.action is LoopAction.REPAIR
                    and transition.reason_code == _PROTOCOL_PREFLIGHT_CORRECTION_REASON
                ):
                    correction_counts[transition.node_id] += 1
                else:
                    loop_counts[(transition.node_id, transition.action)] += 1
        repair_feedback_by_node: dict[str, tuple[Digest, ...]] = {}
        repair_goal_by_node: dict[str, str] = {}
        for node_id, record in records.items():
            if record.status != "pending":
                continue
            repair = self._active_repair_transition(
                run_id,
                node_id,
                graph_digest,
                record.generation,
                record.attempt,
            )
            if repair is not None:
                repair_feedback_by_node[node_id] = repair.evidence_digests
                if repair.reason_code == "ACCEPTED_TASK_REVIEW_FEEDBACK":
                    repair_goal_by_node[node_id] = self._task_review_repair_goal(
                        nodes[node_id], repair.evidence_digests
                    )
                elif repair.reason_code in {
                    "ACCEPTED_NODE_EVALUATION_FEEDBACK",
                    _PROTOCOL_PREFLIGHT_CORRECTION_REASON,
                }:
                    repair_goal_by_node[node_id] = self._node_evaluation_repair_goal(
                        nodes[node_id], repair.evidence_digests
                    )
                elif repair.reason_code == "ACCEPTED_PARENT_EVALUATION_FEEDBACK":
                    repair_goal_by_node[node_id] = self._parent_evaluation_repair_goal(
                        nodes[node_id], repair.evidence_digests
                    )
        active: dict[
            Future[NodeExecutionResult],
            tuple[
                str,
                Node,
                WorkerRequest,
                NodeReservationRecord,
                ExecutionStrategy,
                float,
            ],
        ] = {}
        run_started_at = min(ensure_utc(item.created_at) for item in records.values())
        timeout_profiles: dict[Future[NodeExecutionResult], WorkerTimeoutProfileRecord] = {}
        attempt_supervisors: dict[Future[NodeExecutionResult], WorkerAttemptSupervisor] = {}
        required_retry_strategies: dict[str, ExecutionStrategy] = {}
        watchdog_signals: dict[Future[NodeExecutionResult], float] = {}
        cancellation_signals: dict[Future[NodeExecutionResult], tuple[float, bool]] = {}
        cleanup_grace_seconds = 2.0
        stop_action: Literal["pause", "cancel"] | None = None
        limits: dict[str, int | float] = {
            "worker_turns": graph_run.max_worker_turns,
            "processes": graph_run.max_processes,
            "wall_seconds": graph_run.max_wall_seconds,
            "artifact_bytes": graph_run.max_artifact_bytes,
        }
        with _ContainmentThreadPoolExecutor(
            max_workers=self.max_concurrency,
            thread_name_prefix=f"fleet-{run_id[:24]}",
        ) as pool:
            while active or any(item.status == "pending" for item in records.values()):
                self._heartbeat_run_owner_if_due()
                if stop_action is None:
                    observed = self.store.control(run_id)
                    if observed == "pause" or observed == "cancel":
                        stop_action = cast(Literal["pause", "cancel"], observed)
                        self.store.put(
                            "graph_control_fact_v2",
                            GraphControlFact(
                                id=identifier("graph-control"),
                                run_id=run_id,
                                created_at=now(),
                                action=stop_action,
                                generation=(
                                    graph_run.generation + 1
                                    if stop_action == "cancel"
                                    else graph_run.generation
                                ),
                            ),
                            run_id=run_id,
                        )
                        if stop_action == "cancel":
                            for future, active_item in tuple(active.items()):
                                if future in cancellation_signals:
                                    continue
                                active_node_id, active_node, active_request, *_rest = active_item
                                propagated = True
                                try:
                                    self.store.request_control(active_request.run_id, "cancel")
                                except (OSError, RuntimeError, ValueError):
                                    propagated = False
                                cancellation_signals[future] = (monotonic(), propagated)
                                self.store.put(
                                    "node_control_propagation_v2",
                                    NodeControlPropagationRecord(
                                        id=identifier("node-control-propagation"),
                                        run_id=run_id,
                                        created_at=now(),
                                        graph_run_id=run_id,
                                        node_id=active_node_id,
                                        child_run_id=active_request.run_id,
                                        accepted_graph_revision_digest=graph_digest,
                                        generation=active_node.generation,
                                        attempt=active_node.attempt,
                                        propagated=propagated,
                                        cleanup_confirmed=False,
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
                        graph_run = graph_run.model_copy(
                            update={
                                "status": "cancelled",
                                "generation": graph_run.generation + 1,
                                "failure_code": "GRAPH_CANCELLED",
                            }
                        )
                        self._save_run(graph_run)
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
                    child_run_id = _node_worker_run_id(run_id, node)
                    try:
                        route = self._route(
                            run_id,
                            node,
                            predecessors[node_id],
                            graph_digest,
                            effective_policy_digest,
                            harness_digest,
                            independent_node_assessment=independent_node_assessment,
                        )
                    except (RoutingError, ValueError):
                        records[node_id] = self._advance(
                            records[node_id],
                            status="failed",
                            failure_code="NO_ELIGIBLE_STRATEGY",
                        )
                        continue
                    expected_retry_strategy = required_retry_strategies.get(node_id)
                    if (
                        expected_retry_strategy is not None
                        and route.selected_strategy != expected_retry_strategy
                    ):
                        records[node_id] = self._advance(
                            records[node_id],
                            status="failed",
                            failure_code="TIMEOUT_RETRY_STRATEGY_CHANGED",
                        )
                        continue
                    required_retry_strategies.pop(node_id, None)
                    self.store.put("node_route_v2", route, run_id=run_id)
                    records[node_id] = self._advance(
                        records[node_id],
                        status="routed",
                        route_digest=route.content_digest,
                    )
                    persisted_profile = route.assessment.semantic_profile
                    scope = (
                        SemanticScope.BOUNDED
                        if persisted_profile is None
                        else persisted_profile.scope
                    )
                    reasoning_class = (
                        SemanticReasoningClass.MECHANICAL
                        if persisted_profile is None
                        else persisted_profile.reasoning_class
                    )
                    timeout_rule = self.worker_supervision_policy.select(
                        scope, reasoning_class, route.assessment.scale
                    )
                    elapsed_run_seconds = max(
                        0.0,
                        (ensure_utc(self.clock()) - run_started_at).total_seconds(),
                    )
                    remaining_wall_seconds = max(
                        0.0, graph_run.max_wall_seconds - elapsed_run_seconds
                    )
                    timeout_profile = select_node_timeout(
                        id=identifier("worker-timeout-profile"),
                        run_id=run_id,
                        created_at=self.clock(),
                        graph_run_id=run_id,
                        node_id=node.id,
                        child_run_id=child_run_id,
                        accepted_graph_revision_digest=graph_digest,
                        generation=node.generation,
                        attempt=node.attempt,
                        operator_config_digest=(
                            self.operator_config_digest
                            or canonical_digest(self.worker_supervision_policy)
                        ),
                        rule=timeout_rule,
                        profile=persisted_profile,
                        scale=route.assessment.scale,
                        accepted_node_timeout_seconds=node.resource_budget.wall_seconds,
                        adapter_timeout_seconds=(
                            self.adapter_timeout_seconds or graph_run.max_wall_seconds
                        ),
                        policy_timeout_seconds=graph_run.execution_policy.max_wall_seconds,
                        remaining_run_timeout_seconds=remaining_wall_seconds,
                    )
                    self.store.put("worker_timeout_profile_v2", timeout_profile, run_id=run_id)
                    denied_authorities = inadequate_authorities(timeout_profile)
                    if denied_authorities:
                        self.store.put(
                            "worker_budget_preflight_v2",
                            WorkerBudgetPreflightRecord(
                                id=identifier("worker-budget-preflight"),
                                run_id=run_id,
                                created_at=self.clock(),
                                graph_run_id=run_id,
                                node_id=node.id,
                                child_run_id=child_run_id,
                                accepted_graph_revision_digest=graph_digest,
                                generation=node.generation,
                                attempt=node.attempt,
                                timeout_profile_digest=_required_digest(
                                    timeout_profile.content_digest
                                ),
                                denied_authorities=denied_authorities,
                            ),
                            run_id=run_id,
                        )
                        records[node_id] = self._advance(
                            records[node_id],
                            status="failed",
                            failure_code=StableFailureCode.WORKER_BUDGET_INADEQUATE.value,
                        )
                        continue

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
                    predecessor_context = self._predecessor_context(
                        tuple(records[parent] for parent in predecessors[node_id]),
                        graph_digest,
                        graph_run.generation,
                    )
                    if predecessor_context is None:
                        records[node_id] = self._advance(
                            records[node_id],
                            status="failed",
                            failure_code=StableFailureCode.CONTEXT_INSUFFICIENT.value,
                        )
                        continue
                    predecessor_outputs, predecessor_evidence_digests = predecessor_context
                    prior_results = tuple(
                        _required_digest(item.worker_result_digest) for item in predecessor_outputs
                    )
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
                            failure_code=StableFailureCode.CONTEXT_INSUFFICIENT.value,
                        )
                        continue
                    request = WorkerRequest(
                        id=identifier("worker-request"),
                        run_id=_node_worker_run_id(run_id, node),
                        created_at=now(),
                        goal=repair_goal_by_node.get(node_id, node.objective or node.name),
                        task_kind=graph_run.goal.task_kind,
                        processes_authorized=graph_run.goal.processes_authorized,
                        completion_criteria=node.completion_criteria,
                        required_capabilities=node.required_capabilities,
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
                        accepted_feedback_digests=repair_feedback_by_node.get(node_id, ()),
                    )
                    manifest = WorkerContextManifest(
                        id=f"{request.id}-context",
                        run_id=run_id,
                        created_at=request.created_at,
                        worker_request_id=request.id,
                        worker_request_digest=_required_digest(request.content_digest),
                        worker_run_id=request.run_id,
                        node_id=node.id,
                        objective_digest=canonical_digest(request.goal),
                        task_kind=request.task_kind,
                        processes_authorized=request.processes_authorized,
                        completion_criteria_digest=canonical_digest(request.completion_criteria),
                        required_capabilities=request.required_capabilities,
                        accepted_graph_revision_digest=graph_digest,
                        generation=node.generation,
                        attempt=node.attempt,
                        workspace_context=request.workspace_context,
                        harness_digest=request.harness_digest,
                        effective_policy_digest=request.effective_policy_digest,
                        remaining_budgets=request.remaining_budgets,
                        predecessor_node_ids=tuple(item.node_id for item in predecessor_outputs),
                        predecessor_result_digests=prior_results,
                        predecessor_evidence_digests=predecessor_evidence_digests,
                        accepted_feedback_digests=request.accepted_feedback_digests,
                        artifact_descriptors=tuple(
                            artifact
                            for item in predecessor_outputs
                            for artifact in item.artifact_descriptors
                        ),
                    )
                    self.store.put("worker_request_v2", request, run_id=run_id)
                    self.store.put("worker_context_manifest_v2", manifest, run_id=run_id)
                    if not self._worker_context_is_valid(
                        node,
                        request,
                        manifest,
                        graph_digest,
                        run_id,
                    ):
                        records[node_id] = self._advance(
                            records[node_id],
                            status="failed",
                            failure_code=StableFailureCode.CONTEXT_INSUFFICIENT.value,
                        )
                        continue
                    contract_failure = _worker_request_contract_failure(node, request)
                    if contract_failure is not None or not self._pre_dispatch_bindings_are_valid(
                        graph_run,
                        node,
                        request,
                        route,
                        predecessors[node_id],
                    ):
                        diagnostic = WorkerBoundaryDiagnostic(
                            id=identifier("worker-boundary-diagnostic"),
                            run_id=request.run_id,
                            created_at=self.clock(),
                            adapter=route.selected_strategy.backend,
                            stage="pre_dispatch",
                            code=(StableFailureCode.WORKER_DISPATCH_CONTRACT_CONTRADICTION.value),
                            graph_run_id=run_id,
                            node_id=node.id,
                            accepted_graph_revision_digest=graph_digest,
                            generation=node.generation,
                            attempt=node.attempt,
                            worker_request_id=request.id,
                            worker_request_digest=_required_digest(request.content_digest),
                            exception_message=(
                                contract_failure
                                or (
                                    "pre-dispatch bindings contradict accepted run authority; "
                                    "recreate the route and worker request from the persisted task "
                                    "and effective policy"
                                )
                            ),
                            duration_seconds=0.0,
                            configured_timeout_seconds=node.resource_budget.wall_seconds,
                            effective_timeout_seconds=timeout_profile.effective_timeout_seconds,
                        )
                        self.store.put("worker_boundary_diagnostic_v2", diagnostic, run_id=run_id)
                        records[node_id] = self._advance(
                            records[node_id],
                            status="failed",
                            failure_code=(
                                StableFailureCode.WORKER_DISPATCH_CONTRACT_CONTRADICTION.value
                            ),
                        )
                        continue
                    records[node_id] = self._advance(
                        records[node_id],
                        status="running",
                        worker_request_digest=request.content_digest,
                    )
                    future = pool.submit(self.runner, node, request, route.selected_strategy)
                    timeout_profiles[future] = timeout_profile
                    attempt_supervisors[future] = WorkerAttemptSupervisor(
                        timeout_profile,
                        heartbeat_interval_seconds=(
                            self.worker_supervision_policy.heartbeat_interval_seconds
                        ),
                        no_progress_threshold_seconds=(
                            self.worker_supervision_policy.no_progress_threshold_seconds
                        ),
                        max_heartbeat_records=(
                            self.worker_supervision_policy.max_heartbeat_records
                        ),
                    )
                    active[future] = (
                        node_id,
                        node,
                        request,
                        reservation,
                        route.selected_strategy,
                        monotonic(),
                    )

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
                pending_deadlines = tuple(
                    active_item[5] + timeout_profiles[future].effective_timeout_seconds
                    for future, active_item in active.items()
                    if future not in watchdog_signals
                )
                wait_timeout = (
                    0.1
                    if not pending_deadlines
                    else max(0.0, min(0.1, min(pending_deadlines) - monotonic()))
                )
                completed, _pending = wait(
                    tuple(active), timeout=wait_timeout, return_when=FIRST_COMPLETED
                )
                observed_at = monotonic()
                for future, active_item in tuple(active.items()):
                    _node_id, _node, request, *_middle, started_at = active_item
                    heartbeat = attempt_supervisors[future].sample(
                        self._observe_worker_attempt(request),
                        elapsed_seconds=max(0.0, observed_at - started_at),
                        observed_at=self.clock(),
                        force=future.done(),
                    )
                    if heartbeat is not None:
                        self.store.put("worker_attempt_heartbeat_v2", heartbeat, run_id=run_id)
                if stop_action is None and self.store.control(run_id) == "cancel":
                    stop_action = "cancel"
                    self.store.put(
                        "graph_control_fact_v2",
                        GraphControlFact(
                            id=identifier("graph-control"),
                            run_id=run_id,
                            created_at=now(),
                            action="cancel",
                            generation=graph_run.generation + 1,
                        ),
                        run_id=run_id,
                    )
                    for future, active_item in tuple(active.items()):
                        active_node_id, active_node, active_request, *_rest = active_item
                        propagated = True
                        try:
                            self.store.request_control(active_request.run_id, "cancel")
                        except (OSError, RuntimeError, ValueError):
                            propagated = False
                        cancellation_signals[future] = (monotonic(), propagated)
                        self.store.put(
                            "node_control_propagation_v2",
                            NodeControlPropagationRecord(
                                id=identifier("node-control-propagation"),
                                run_id=run_id,
                                created_at=now(),
                                graph_run_id=run_id,
                                node_id=active_node_id,
                                child_run_id=active_request.run_id,
                                accepted_graph_revision_digest=graph_digest,
                                generation=active_node.generation,
                                attempt=active_node.attempt,
                                propagated=propagated,
                                cleanup_confirmed=False,
                            ),
                            run_id=run_id,
                        )
                for future, active_item in tuple(active.items()):
                    if future.done() or future in watchdog_signals:
                        continue
                    active_node_id, active_node, active_request, *_middle, started_at = active_item
                    if observed_at < (
                        started_at + timeout_profiles[future].effective_timeout_seconds
                    ):
                        continue
                    signal_outcome: Literal["signal_sent", "cleanup_failed"] = "signal_sent"
                    try:
                        self.store.request_control(active_request.run_id, "cancel")
                    except (OSError, RuntimeError, ValueError):
                        signal_outcome = "cleanup_failed"
                    watchdog_signals[future] = observed_at
                    self.store.put(
                        "node_watchdog_v2",
                        NodeWatchdogRecord(
                            id=identifier("node-watchdog"),
                            run_id=run_id,
                            created_at=now(),
                            graph_run_id=run_id,
                            node_id=active_node_id,
                            child_run_id=active_request.run_id,
                            accepted_graph_revision_digest=graph_digest,
                            generation=active_node.generation,
                            attempt=active_node.attempt,
                            allowance_seconds=timeout_profiles[future].effective_timeout_seconds,
                            cleanup_grace_seconds=cleanup_grace_seconds,
                            outcome=signal_outcome,
                            timeout_profile_digest=timeout_profiles[future].content_digest,
                        ),
                        run_id=run_id,
                    )
                for future, active_item in tuple(active.items()):
                    if future.done():
                        continue
                    signal_started = watchdog_signals.get(future)
                    if (
                        signal_started is None
                        or observed_at - signal_started <= cleanup_grace_seconds
                    ):
                        continue
                    active.pop(future)
                    active_node_id, active_node, active_request, *_unused = active_item
                    self.store.put(
                        "node_watchdog_v2",
                        NodeWatchdogRecord(
                            id=identifier("node-watchdog"),
                            run_id=run_id,
                            created_at=now(),
                            graph_run_id=run_id,
                            node_id=active_node_id,
                            child_run_id=active_request.run_id,
                            accepted_graph_revision_digest=graph_digest,
                            generation=active_node.generation,
                            attempt=active_node.attempt,
                            allowance_seconds=timeout_profiles[future].effective_timeout_seconds,
                            cleanup_grace_seconds=cleanup_grace_seconds,
                            outcome="cleanup_failed",
                            timeout_profile_digest=timeout_profiles[future].content_digest,
                        ),
                        run_id=run_id,
                    )
                    records[active_node_id] = self._advance(
                        records[active_node_id],
                        status="failed",
                        failure_code="WATCHDOG_TIMEOUT:CLEANUP_UNCONFIRMED",
                    )
                for future in sorted(completed, key=lambda item: active[item][0]):
                    self._assert_run_owner("consume_child_result")
                    node_id, node, request, reservation, strategy, started_at = active.pop(future)
                    try:
                        result = future.result()
                        if future in watchdog_signals:
                            self.store.put(
                                "node_watchdog_v2",
                                NodeWatchdogRecord(
                                    id=identifier("node-watchdog"),
                                    run_id=run_id,
                                    created_at=now(),
                                    graph_run_id=run_id,
                                    node_id=node_id,
                                    child_run_id=request.run_id,
                                    accepted_graph_revision_digest=graph_digest,
                                    generation=node.generation,
                                    attempt=node.attempt,
                                    allowance_seconds=timeout_profiles[
                                        future
                                    ].effective_timeout_seconds,
                                    cleanup_grace_seconds=cleanup_grace_seconds,
                                    timeout_profile_digest=timeout_profiles[future].content_digest,
                                    outcome=(
                                        "cleanup_confirmed"
                                        if (
                                            monotonic() - watchdog_signals[future]
                                            <= cleanup_grace_seconds
                                            and not (
                                                result.worker_result.failure is not None
                                                and result.worker_result.failure.code
                                                is StableFailureCode.PROCESS_GROUP_CLEANUP_FAILED
                                            )
                                        )
                                        else "cleanup_failed"
                                    ),
                                ),
                                run_id=run_id,
                            )
                            remaining = cast(
                                Mapping[str, int | float], reservation.remaining_budgets
                            )
                            retry_cap = min(
                                nodes[node_id].retry_limit,
                                accepted.graph.budget.max_retries,
                            )
                            retry_count = loop_counts[(node_id, LoopAction.RETRY)]
                            retry_within_policy = (
                                strategy.id in graph_run.allowed_strategy_ids
                                and strategy.backend in graph_run.allowed_backends
                            )
                            retry_within_counters = retry_count < retry_cap
                            retry_within_resources = _node_resources_remain(node, remaining)
                            recovery_action = timeout_recovery_action(
                                retry_within_policy=retry_within_policy,
                                retry_within_counters=retry_within_counters,
                                retry_within_resource_budgets=retry_within_resources,
                                replan_authorized=graph_run.replan_count < graph_run.max_replans,
                            )
                            self.store.put(
                                "timeout_recovery_v2",
                                TimeoutRecoveryRecord(
                                    id=identifier("timeout-recovery"),
                                    run_id=run_id,
                                    created_at=now(),
                                    graph_run_id=run_id,
                                    node_id=node_id,
                                    child_run_id=request.run_id,
                                    accepted_graph_revision_digest=graph_digest,
                                    timeout_profile_digest=_required_digest(
                                        timeout_profiles[future].content_digest
                                    ),
                                    source_generation=node.generation,
                                    source_attempt=node.attempt,
                                    action=recovery_action,
                                    routing_mode=graph_run.routing_mode.value,
                                    source_strategy_id=strategy.id,
                                    source_model=strategy.model,
                                    source_backend=strategy.backend,
                                    retry_strategy_id=(
                                        strategy.id
                                        if recovery_action == "same_strategy_retry"
                                        else None
                                    ),
                                    retry_model=(
                                        strategy.model
                                        if recovery_action == "same_strategy_retry"
                                        else None
                                    ),
                                    retry_backend=(
                                        strategy.backend
                                        if recovery_action == "same_strategy_retry"
                                        else None
                                    ),
                                    retry_within_policy=retry_within_policy,
                                    retry_within_counters=retry_within_counters,
                                    retry_within_resource_budgets=retry_within_resources,
                                    normal_acceptance_required=(
                                        recovery_action == "replan_required"
                                    ),
                                ),
                                run_id=run_id,
                            )
                            if recovery_action == "same_strategy_retry":
                                self._save_loop_transition(
                                    LoopTransitionRecord(
                                        id=identifier("loop-transition"),
                                        run_id=run_id,
                                        created_at=now(),
                                        action=LoopAction.RETRY,
                                        reason_code="WATCHDOG_TIMEOUT",
                                        accepted_graph_revision_digest=graph_digest,
                                        generation=node.generation,
                                        attempt=node.attempt,
                                        node_id=node_id,
                                        worker_request_digest=request.content_digest,
                                        worker_result_digest=result.worker_result.content_digest,
                                        consumed=retry_count + 1,
                                        limit=retry_cap,
                                    )
                                )
                                loop_counts[(node_id, LoopAction.RETRY)] += 1
                                required_retry_strategies[node_id] = strategy
                                records[node_id] = NodeExecutionRecord(
                                    id=identifier("node-execution"),
                                    run_id=run_id,
                                    created_at=now(),
                                    transitioned_at=self.clock(),
                                    node_id=node_id,
                                    accepted_graph_revision_digest=graph_digest,
                                    generation=graph_run.generation,
                                    attempt=records[node_id].attempt + 1,
                                    sequence=0,
                                    status="pending",
                                    failure_code="RETRY_AFTER:WATCHDOG_TIMEOUT",
                                )
                                self._save_node(records[node_id])
                            else:
                                records[node_id] = self._advance(
                                    records[node_id],
                                    status="failed",
                                    failure_code="WATCHDOG_TIMEOUT",
                                )
                            continue
                        if future in cancellation_signals:
                            self.store.put(
                                "stale_node_result_v2",
                                StaleNodeResultRecord(
                                    id=identifier("stale-node-result"),
                                    run_id=run_id,
                                    created_at=now(),
                                    node_id=node.id,
                                    accepted_graph_revision_digest=graph_digest,
                                    result_generation=request.generation,
                                    authoritative_generation=graph_run.generation + 1,
                                    attempt=request.attempt,
                                    worker_request_digest=_required_digest(request.content_digest),
                                    worker_result_digest=_required_digest(
                                        result.worker_result.content_digest
                                    ),
                                ),
                                run_id=run_id,
                            )
                            cleanup_confirmed = not (
                                result.worker_result.failure is not None
                                and result.worker_result.failure.code
                                is StableFailureCode.PROCESS_GROUP_CLEANUP_FAILED
                            )
                            self.store.put(
                                "node_control_propagation_v2",
                                NodeControlPropagationRecord(
                                    id=identifier("node-control-propagation"),
                                    run_id=run_id,
                                    created_at=now(),
                                    graph_run_id=run_id,
                                    node_id=node_id,
                                    child_run_id=request.run_id,
                                    accepted_graph_revision_digest=graph_digest,
                                    generation=node.generation,
                                    attempt=node.attempt,
                                    propagated=cancellation_signals[future][1],
                                    cleanup_confirmed=cleanup_confirmed,
                                ),
                                run_id=run_id,
                            )
                            records[node_id] = self._advance(
                                records[node_id],
                                status="cancelled",
                                failure_code="GRAPH_CANCELLED",
                            )
                            continue
                        self._persist_result(
                            node,
                            request,
                            result,
                            records,
                            evidence_by_node,
                            graph_digest,
                        )
                        current = records[node_id]
                        post_result_feedback = tuple(
                            item.content_digest
                            for item in self.store.list_records(
                                "action_result_v2", ExecutionResult, run_id=request.run_id
                            )
                            if item.status != "succeeded" and item.content_digest is not None
                        )
                        boundary_feedback = (
                            ()
                            if result.worker_result.boundary_diagnostic is None
                            else (
                                _required_digest(
                                    result.worker_result.boundary_diagnostic.content_digest
                                ),
                            )
                        )
                        feedback = tuple(
                            item
                            for item in (
                                current.evidence_digest,
                                current.evaluator_digest,
                                *current.verification_result_digests,
                                *post_result_feedback,
                                *boundary_feedback,
                            )
                            if item is not None
                        )
                        if current.status == "passed":
                            review_decision: TaskReviewDecision | None = None
                            if self.independent_task_review:
                                review_decision, feedback = self._review_verified_result(
                                    graph_run,
                                    node,
                                    request,
                                    result,
                                    current,
                                    graph_digest,
                                )
                            if review_decision is None or (
                                review_decision.action is TaskReviewAction.PASS
                            ):
                                self._save_loop_transition(
                                    LoopTransitionRecord(
                                        id=identifier("loop-transition"),
                                        run_id=run_id,
                                        created_at=now(),
                                        action=LoopAction.PASS,
                                        reason_code=(
                                            "NODE_EVALUATION_PASS"
                                            if review_decision is None
                                            else "TASK_REVIEW_PASS"
                                        ),
                                        accepted_graph_revision_digest=graph_digest,
                                        generation=node.generation,
                                        attempt=node.attempt,
                                        node_id=node_id,
                                        worker_request_digest=request.content_digest,
                                        worker_result_digest=current.worker_result_digest,
                                        evidence_digests=feedback,
                                        consumed=0,
                                        limit=0,
                                    )
                                )
                            elif review_decision.action is TaskReviewAction.REPAIR:
                                repair_count = loop_counts[(node_id, LoopAction.REPAIR)]
                                repair_limit = graph_run.max_repairs
                                remaining = cast(
                                    Mapping[str, int | float], reservation.remaining_budgets
                                )
                                resources_available = _node_resources_remain(node, remaining)
                                if repair_count < repair_limit and resources_available:
                                    transition = LoopTransitionRecord(
                                        id=identifier("loop-transition"),
                                        run_id=run_id,
                                        created_at=now(),
                                        action=LoopAction.REPAIR,
                                        reason_code="ACCEPTED_TASK_REVIEW_FEEDBACK",
                                        accepted_graph_revision_digest=graph_digest,
                                        generation=node.generation,
                                        attempt=node.attempt,
                                        node_id=node_id,
                                        worker_request_digest=request.content_digest,
                                        worker_result_digest=current.worker_result_digest,
                                        evidence_digests=feedback,
                                        consumed=repair_count + 1,
                                        limit=repair_limit,
                                    )
                                    self._save_loop_transition(transition)
                                    loop_counts[(node_id, LoopAction.REPAIR)] += 1
                                    repair_feedback_by_node[node_id] = feedback
                                    repair_goal_by_node[node_id] = self._task_review_repair_goal(
                                        node, feedback
                                    )
                                    records[node_id] = NodeExecutionRecord(
                                        id=identifier("node-execution"),
                                        run_id=run_id,
                                        created_at=now(),
                                        transitioned_at=self.clock(),
                                        node_id=node_id,
                                        accepted_graph_revision_digest=graph_digest,
                                        generation=graph_run.generation,
                                        attempt=current.attempt + 1,
                                        sequence=0,
                                        status="pending",
                                        failure_code="REPAIR_AFTER:TASK_REVIEW_BLOCKED",
                                    )
                                    self._save_node(records[node_id])
                                else:
                                    reason = (
                                        "REPAIR_RESOURCE_BUDGET_EXHAUSTED"
                                        if repair_count < repair_limit
                                        else "REPAIR_BUDGET_EXHAUSTED"
                                    )
                                    self._save_loop_transition(
                                        LoopTransitionRecord(
                                            id=identifier("loop-transition"),
                                            run_id=run_id,
                                            created_at=now(),
                                            action=LoopAction.ESCALATE,
                                            reason_code=reason,
                                            accepted_graph_revision_digest=graph_digest,
                                            generation=node.generation,
                                            attempt=node.attempt,
                                            node_id=node_id,
                                            worker_request_digest=request.content_digest,
                                            worker_result_digest=current.worker_result_digest,
                                            evidence_digests=feedback,
                                            consumed=repair_count,
                                            limit=repair_limit,
                                        )
                                    )
                                    records[node_id] = self._advance(
                                        current,
                                        status="failed",
                                        failure_code=f"LOOP_ESCALATED:{reason}",
                                    )
                            else:
                                action = (
                                    LoopAction.ESCALATE
                                    if review_decision.action is TaskReviewAction.ESCALATE
                                    else LoopAction.FAIL
                                )
                                self._save_loop_transition(
                                    LoopTransitionRecord(
                                        id=identifier("loop-transition"),
                                        run_id=run_id,
                                        created_at=now(),
                                        action=action,
                                        reason_code=review_decision.reason_code,
                                        accepted_graph_revision_digest=graph_digest,
                                        generation=node.generation,
                                        attempt=node.attempt,
                                        node_id=node_id,
                                        worker_request_digest=request.content_digest,
                                        worker_result_digest=current.worker_result_digest,
                                        evidence_digests=feedback,
                                        consumed=0,
                                        limit=0,
                                    )
                                )
                                records[node_id] = self._advance(
                                    current,
                                    status="failed",
                                    failure_code=(
                                        f"LOOP_ESCALATED:{review_decision.reason_code}"
                                        if action is LoopAction.ESCALATE
                                        else review_decision.reason_code
                                    ),
                                )
                        elif (
                            current.failure_code
                            != StableFailureCode.VERIFICATION_BINDING_INVALID.value
                            and current.evaluator_decision is EvaluationDecision.FAIL
                            and (
                                result.worker_result.status == "succeeded"
                                or current.failure_code
                                in {
                                    StableFailureCode.WORKER_PROTOCOL_ERROR.value,
                                    StableFailureCode.WORKER_EMPTY_OUTPUT.value,
                                    StableFailureCode.WORKER_STRUCTURED_OUTPUT_MISSING.value,
                                }
                            )
                        ):
                            protocol_correction = (
                                current.failure_code in _PROTOCOL_PREFLIGHT_CORRECTION_FAILURE_CODES
                            )
                            repair_count = (
                                correction_counts[node_id]
                                if protocol_correction
                                else loop_counts[(node_id, LoopAction.REPAIR)]
                            )
                            repair_limit = graph_run.max_repairs
                            remaining = cast(
                                Mapping[str, int | float], reservation.remaining_budgets
                            )
                            resources_available = _node_resources_remain(node, remaining)
                            if repair_count < repair_limit and resources_available:
                                transition = LoopTransitionRecord(
                                    id=identifier("loop-transition"),
                                    run_id=run_id,
                                    created_at=now(),
                                    action=LoopAction.REPAIR,
                                    reason_code=(
                                        _PROTOCOL_PREFLIGHT_CORRECTION_REASON
                                        if protocol_correction
                                        else "ACCEPTED_NODE_EVALUATION_FEEDBACK"
                                    ),
                                    accepted_graph_revision_digest=graph_digest,
                                    generation=node.generation,
                                    attempt=node.attempt,
                                    node_id=node_id,
                                    worker_request_digest=request.content_digest,
                                    worker_result_digest=current.worker_result_digest,
                                    evidence_digests=feedback,
                                    consumed=repair_count + 1,
                                    limit=repair_limit,
                                )
                                self._save_loop_transition(transition)
                                if protocol_correction:
                                    correction_counts[node_id] += 1
                                else:
                                    loop_counts[(node_id, LoopAction.REPAIR)] += 1
                                repair_feedback_by_node[node_id] = feedback
                                repair_goal_by_node[node_id] = self._node_evaluation_repair_goal(
                                    node, feedback
                                )
                                records[node_id] = NodeExecutionRecord(
                                    id=identifier("node-execution"),
                                    run_id=run_id,
                                    created_at=now(),
                                    transitioned_at=self.clock(),
                                    node_id=node_id,
                                    accepted_graph_revision_digest=graph_digest,
                                    generation=graph_run.generation,
                                    attempt=current.attempt + 1,
                                    sequence=0,
                                    status="pending",
                                    failure_code="REPAIR_AFTER:NODE_EVALUATION_NOT_PASS",
                                )
                                self._save_node(records[node_id])
                            else:
                                enabled = repair_limit > 0
                                correctable_failure = current.failure_code in {
                                    StableFailureCode.VERIFICATION_FAILED.value,
                                    StableFailureCode.PATCH_PREFLIGHT_FAILED.value,
                                    StableFailureCode.WORKER_PROTOCOL_ERROR.value,
                                    StableFailureCode.WORKER_EMPTY_OUTPUT.value,
                                    StableFailureCode.WORKER_STRUCTURED_OUTPUT_MISSING.value,
                                }
                                action = (
                                    LoopAction.ESCALATE
                                    if enabled or correctable_failure
                                    else LoopAction.FAIL
                                )
                                reason = (
                                    "REPAIR_RESOURCE_BUDGET_EXHAUSTED"
                                    if repair_count < repair_limit
                                    else "REPAIR_BUDGET_EXHAUSTED"
                                )
                                self._save_loop_transition(
                                    LoopTransitionRecord(
                                        id=identifier("loop-transition"),
                                        run_id=run_id,
                                        created_at=now(),
                                        action=action,
                                        reason_code=(
                                            reason
                                            if enabled or correctable_failure
                                            else "NODE_EVALUATION_NOT_PASS"
                                        ),
                                        accepted_graph_revision_digest=graph_digest,
                                        generation=node.generation,
                                        attempt=node.attempt,
                                        node_id=node_id,
                                        worker_request_digest=request.content_digest,
                                        worker_result_digest=current.worker_result_digest,
                                        evidence_digests=feedback,
                                        consumed=repair_count,
                                        limit=repair_limit,
                                    )
                                )
                                if enabled or correctable_failure:
                                    records[node_id] = self._advance(
                                        current,
                                        failure_code=f"LOOP_ESCALATED:{reason}",
                                    )
                        else:
                            self._save_loop_transition(
                                LoopTransitionRecord(
                                    id=identifier("loop-transition"),
                                    run_id=run_id,
                                    created_at=now(),
                                    action=LoopAction.FAIL,
                                    reason_code=current.failure_code or "NODE_RESULT_REJECTED",
                                    accepted_graph_revision_digest=graph_digest,
                                    generation=node.generation,
                                    attempt=node.attempt,
                                    node_id=node_id,
                                    worker_request_digest=request.content_digest,
                                    worker_result_digest=current.worker_result_digest,
                                    evidence_digests=feedback,
                                    consumed=0,
                                    limit=0,
                                )
                            )
                    except Exception as error:
                        if stop_action == "cancel":
                            records[node_id] = self._advance(
                                records[node_id],
                                status="cancelled",
                                failure_code="GRAPH_CANCELLED",
                            )
                            continue
                        persisted_results = tuple(
                            item
                            for item in self.store.list_records(
                                "worker_result_v2", WorkerResult, run_id=request.run_id
                            )
                            if item.request_digest == request.content_digest
                        )
                        boundary_result = (
                            persisted_results[0] if len(persisted_results) == 1 else None
                        )
                        if boundary_result is not None:
                            # A post-result runtime failure must not erase or replace the
                            # exact worker identity that crossed the boundary successfully.
                            self.store.put("worker_result_v2", boundary_result, run_id=run_id)
                            records[node_id] = self._advance(
                                records[node_id],
                                status="failed",
                                output_generation=node.generation,
                                worker_result_id=boundary_result.id,
                                worker_result_digest=boundary_result.content_digest,
                                failure_code=StableFailureCode.WORKER_BOUNDARY_ERROR.value,
                            )
                        remaining = cast(Mapping[str, int | float], reservation.remaining_budgets)
                        retry_cap = min(
                            nodes[node_id].retry_limit,
                            accepted.graph.budget.max_retries,
                        )
                        retry_count = loop_counts[(node_id, LoopAction.RETRY)]
                        retry_resources_available = _node_resources_remain(node, remaining)
                        retryable = retry_count < retry_cap and retry_resources_available
                        exception_type, exception_message = _sanitized_boundary_exception(error)
                        effective_timeout = max(
                            0.0,
                            min(
                                node.resource_budget.wall_seconds,
                                float(
                                    remaining.get("wall_seconds", node.resource_budget.wall_seconds)
                                ),
                            ),
                        )
                        try:
                            diagnostic = WorkerBoundaryDiagnostic(
                                id=identifier("worker-boundary-diagnostic"),
                                run_id=request.run_id,
                                created_at=now(),
                                adapter=strategy.backend,
                                stage="runner",
                                code=StableFailureCode.WORKER_BOUNDARY_ERROR.value,
                                retryable=retryable,
                                graph_run_id=run_id,
                                node_id=node.id,
                                accepted_graph_revision_digest=graph_digest,
                                generation=node.generation,
                                attempt=node.attempt,
                                worker_request_id=request.id,
                                worker_request_digest=_required_digest(request.content_digest),
                                exception_type=exception_type,
                                exception_message=exception_message,
                                duration_seconds=max(0.0, monotonic() - started_at),
                                configured_timeout_seconds=node.resource_budget.wall_seconds,
                                effective_timeout_seconds=effective_timeout,
                            )
                            self.store.put(
                                "worker_boundary_diagnostic_v2", diagnostic, run_id=run_id
                            )
                        except Exception as diagnostic_error:
                            with suppress(Exception):
                                self.store.put(
                                    "diagnostic_persistence_failure_v2",
                                    DiagnosticPersistenceFailureRecord(
                                        id=identifier("diagnostic-persistence-failure"),
                                        run_id=run_id,
                                        created_at=now(),
                                        graph_run_id=run_id,
                                        node_id=node.id,
                                        child_run_id=request.run_id,
                                        accepted_graph_revision_digest=graph_digest,
                                        generation=node.generation,
                                        attempt=node.attempt,
                                        original_exception_type=exception_type,
                                        diagnostic_exception_type=type(diagnostic_error).__name__,
                                        effective_timeout_seconds=effective_timeout,
                                    ),
                                    run_id=run_id,
                                )
                        if retry_count < retry_cap and retry_resources_available:
                            self._save_loop_transition(
                                LoopTransitionRecord(
                                    id=identifier("loop-transition"),
                                    run_id=run_id,
                                    created_at=now(),
                                    action=LoopAction.RETRY,
                                    reason_code=StableFailureCode.WORKER_BOUNDARY_ERROR.value,
                                    accepted_graph_revision_digest=graph_digest,
                                    generation=node.generation,
                                    attempt=node.attempt,
                                    node_id=node_id,
                                    worker_request_digest=request.content_digest,
                                    worker_result_digest=(
                                        None
                                        if boundary_result is None
                                        else boundary_result.content_digest
                                    ),
                                    consumed=retry_count + 1,
                                    limit=retry_cap,
                                )
                            )
                            loop_counts[(node_id, LoopAction.RETRY)] += 1
                            records[node_id] = NodeExecutionRecord(
                                id=identifier("node-execution"),
                                run_id=run_id,
                                created_at=now(),
                                transitioned_at=self.clock(),
                                node_id=node_id,
                                accepted_graph_revision_digest=graph_digest,
                                generation=graph_run.generation,
                                attempt=records[node_id].attempt + 1,
                                sequence=0,
                                status="pending",
                                failure_code=(
                                    f"RETRY_AFTER:{StableFailureCode.WORKER_BOUNDARY_ERROR.value}"
                                ),
                            )
                            self._save_node(records[node_id])
                        else:
                            enabled = retry_cap > 0
                            reason = (
                                "RETRY_RESOURCE_BUDGET_EXHAUSTED"
                                if retry_count < retry_cap
                                else "RETRY_BUDGET_EXHAUSTED"
                            )
                            self._save_loop_transition(
                                LoopTransitionRecord(
                                    id=identifier("loop-transition"),
                                    run_id=run_id,
                                    created_at=now(),
                                    action=(LoopAction.ESCALATE if enabled else LoopAction.FAIL),
                                    reason_code=(
                                        reason
                                        if enabled
                                        else StableFailureCode.WORKER_BOUNDARY_ERROR.value
                                    ),
                                    accepted_graph_revision_digest=graph_digest,
                                    generation=node.generation,
                                    attempt=node.attempt,
                                    node_id=node_id,
                                    worker_request_digest=request.content_digest,
                                    worker_result_digest=(
                                        None
                                        if boundary_result is None
                                        else boundary_result.content_digest
                                    ),
                                    consumed=retry_count,
                                    limit=retry_cap,
                                )
                            )
                            records[node_id] = self._advance(
                                records[node_id],
                                status="failed",
                                failure_code=StableFailureCode.WORKER_BOUNDARY_ERROR.value,
                            )

        if stop_action == "cancel":
            for node_id in sorted(nodes):
                if records[node_id].status in {"pending", "routed", "running", "blocked"}:
                    records[node_id] = self._advance(
                        records[node_id],
                        status="cancelled",
                        failure_code="GRAPH_CANCELLED",
                    )
            cancelled_run = graph_run.model_copy(
                update={
                    "status": "cancelled",
                    "generation": graph_run.generation + 1,
                    "failure_code": "GRAPH_CANCELLED",
                }
            )
            self._save_run(cancelled_run)
            return cancelled_run
        authoritative = self.store.get("graph_run_v2", run_id, GraphRunRecord)
        if authoritative.generation != graph_run.generation:
            return authoritative
        if self.independent_task_review:
            for node_id, record in tuple(records.items()):
                if record.status == "passed" and not self._task_review_pass_is_authoritative(
                    record
                ):
                    records[node_id] = self._advance(
                        record,
                        status="failed",
                        failure_code="TASK_REVIEW_INCOMPLETE",
                    )
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
        stable_codes = {item.value for item in StableFailureCode}
        exact_failures = {
            item.failure_code
            for item in records.values()
            if item.failure_code in stable_codes
            or (item.failure_code is not None and item.failure_code.startswith("LOOP_ESCALATED:"))
        }
        graph_run = graph_run.model_copy(
            update={
                "status": "completed" if completed_ok else "failed",
                "goal_evaluator_digest": goal_evaluation.content_digest,
                "failure_code": (
                    None
                    if completed_ok
                    else (
                        next(iter(exact_failures))
                        if len(exact_failures) == 1
                        else "GOAL_OR_NODE_EVALUATION_FAILED"
                    )
                ),
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
        # A failed gate never creates GraphRunRecord. Preserve its Goal under the
        # run identity (not Goal.id, which callers may legitimately reuse).
        goal_record = PreAcceptanceGoalRecord(
            id=proposal.run_id,
            run_id=proposal.run_id,
            created_at=now(),
            goal=goal,
            goal_digest=canonical_digest(goal),
        )
        if not self.store.put_once("pre_acceptance_goal_v2", goal_record, run_id=proposal.run_id):
            stored_goal = self.store.get(
                "pre_acceptance_goal_v2", proposal.run_id, PreAcceptanceGoalRecord
            )
            if stored_goal.goal != goal or stored_goal.goal_digest != goal_record.goal_digest:
                raise ValueError("pre-acceptance Goal changed for an existing run")
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
                or revised.planner_routing != proposal.planner_routing
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
                raise PlanReviewInvocationError(
                    PlanReviewFailureKind.STALE_BINDING,
                    "plan review returned stale or mismatched bindings",
                )
            attempt = _completed_plan_review_attempt(trusted)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            attempt = _failed_plan_review_attempt(
                goal, proposal, review_round, self.plan_reviewer.strategy
            )
            self.store.put("plan_review_attempt_v2", attempt, run_id=proposal.run_id)
            failure = _plan_review_failure_evidence(attempt, error)
            self.store.put("plan_review_failure_evidence_v2", failure, run_id=proposal.run_id)
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
            context_manifests=tuple(
                sorted(
                    self.store.list_records(
                        "worker_context_manifest_v2", WorkerContextManifest, run_id=run_id
                    ),
                    key=lambda item: (item.generation, item.attempt, item.node_id),
                )
            ),
            semantic_assessments=tuple(
                sorted(
                    self.store.list_records(
                        "node_semantic_assessment_v2",
                        NodeSemanticAssessmentRecord,
                        run_id=run_id,
                    ),
                    key=lambda item: (item.accepted_graph_revision_digest, item.node_id),
                )
            ),
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
            loop_transitions=tuple(
                sorted(
                    self.store.list_records(
                        "loop_transition_v2", LoopTransitionRecord, run_id=run_id
                    ),
                    key=lambda item: (
                        item.generation,
                        item.created_at,
                        item.id,
                    ),
                )
            ),
            task_review_requests=tuple(
                sorted(
                    self.store.list_records(
                        "task_review_request_v2", TaskReviewRequest, run_id=run_id
                    ),
                    key=lambda item: (item.generation, item.attempt, item.node_id),
                )
            ),
            task_review_results=tuple(
                sorted(
                    self.store.list_records(
                        "task_review_result_v2", TaskReviewResult, run_id=run_id
                    ),
                    key=lambda item: (item.generation, item.attempt, item.node_id),
                )
            ),
            task_review_decisions=tuple(
                sorted(
                    self.store.list_records(
                        "task_review_decision_v2", TaskReviewDecision, run_id=run_id
                    ),
                    key=lambda item: (item.generation, item.attempt, item.node_id),
                )
            ),
            stale_task_review_results=tuple(
                sorted(
                    self.store.list_records(
                        "stale_task_review_result_v2", StaleTaskReviewResult, run_id=run_id
                    ),
                    key=lambda item: (
                        item.expected_generation,
                        item.expected_attempt,
                        item.node_id,
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

    def prepare_parent_repair(
        self,
        run_id: Identifier,
        evaluation_digest: Digest,
    ) -> bool:
        """Durably schedule one exact single-node repair from failed parent evidence."""

        from .graph_evaluation import ParentCandidateEvaluationRecord

        run = self.store.get("graph_run_v2", run_id, GraphRunRecord)
        acceptance = self.store.list_records(
            "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run_id
        )[-1]
        graph = acceptance.accepted_revision.graph
        writing_nodes = tuple(
            node for node in graph.nodes if "edit_intent" in node.required_capabilities
        )
        evaluations = tuple(
            item
            for item in self.store.list_records(
                "parent_candidate_evaluation_v2",
                ParentCandidateEvaluationRecord,
                run_id=run_id,
            )
            if item.content_digest == evaluation_digest
        )
        if (
            run.status != "failed"
            or run.failure_code is None
            or len(writing_nodes) != 1
            or len(evaluations) != 1
            or evaluations[0].status != "failed"
            or evaluations[0].accepted_graph_revision_digest != run.accepted_graph_revision_digest
        ):
            return False
        node = writing_nodes[0]
        repairs = tuple(
            item
            for item in self.store.list_records(
                "loop_transition_v2", LoopTransitionRecord, run_id=run_id
            )
            if item.action is LoopAction.REPAIR
            and item.node_id == node.id
            and item.reason_code != _PROTOCOL_PREFLIGHT_CORRECTION_REASON
        )
        replay = self.replay(run_id)
        prior = next((item for item in replay.nodes if item.node_id == node.id), None)
        if (
            prior is None
            or prior.status != "passed"
            or prior.worker_request_digest is None
            or prior.worker_result_digest is None
        ):
            return False
        evaluation = evaluations[0]
        feedback = tuple(
            dict.fromkeys(
                (
                    evaluation_digest,
                    evaluation.request_digest,
                    *evaluation.verification_result_digests,
                    *evaluation.evaluation_ledger_digests,
                )
            )
        )
        reservations = tuple(
            item
            for item in replay.reservations
            if item.node_id == node.id and item.attempt == prior.attempt
        )
        resources_available = bool(reservations) and _node_resources_remain(
            node, cast(Mapping[str, int | float], reservations[-1].remaining_budgets)
        )
        if len(repairs) >= run.max_repairs or not resources_available:
            reason = (
                "REPAIR_BUDGET_EXHAUSTED"
                if len(repairs) >= run.max_repairs
                else "REPAIR_RESOURCE_BUDGET_EXHAUSTED"
            )
            self._save_loop_transition(
                LoopTransitionRecord(
                    id=identifier("loop-transition"),
                    run_id=run_id,
                    created_at=now(),
                    action=LoopAction.ESCALATE,
                    reason_code=reason,
                    accepted_graph_revision_digest=run.accepted_graph_revision_digest,
                    generation=run.generation,
                    attempt=prior.attempt,
                    node_id=node.id,
                    worker_request_digest=prior.worker_request_digest,
                    worker_result_digest=prior.worker_result_digest,
                    evidence_digests=feedback,
                    consumed=len(repairs),
                    limit=run.max_repairs,
                )
            )
            self._save_run(run.model_copy(update={"failure_code": f"LOOP_ESCALATED:{reason}"}))
            return False
        self._save_loop_transition(
            LoopTransitionRecord(
                id=identifier("loop-transition"),
                run_id=run_id,
                created_at=now(),
                action=LoopAction.REPAIR,
                reason_code="ACCEPTED_PARENT_EVALUATION_FEEDBACK",
                accepted_graph_revision_digest=run.accepted_graph_revision_digest,
                generation=run.generation,
                attempt=prior.attempt,
                node_id=node.id,
                worker_request_digest=prior.worker_request_digest,
                worker_result_digest=prior.worker_result_digest,
                evidence_digests=feedback,
                consumed=len(repairs) + 1,
                limit=run.max_repairs,
            )
        )
        self._save_node(
            NodeExecutionRecord(
                id=identifier("node-execution"),
                run_id=run_id,
                created_at=now(),
                transitioned_at=self.clock(),
                node_id=node.id,
                accepted_graph_revision_digest=run.accepted_graph_revision_digest,
                generation=run.generation,
                attempt=prior.attempt + 1,
                sequence=0,
                status="pending",
                failure_code="REPAIR_AFTER:PARENT_EVALUATION_FAILED",
            )
        )
        self._save_run(
            run.model_copy(update={"status": "paused", "failure_code": "PARENT_REPAIR_PENDING"})
        )
        return True

    def _validate_planner_routing(
        self,
        goal: Goal,
        proposal: ProposedGraph,
        capabilities: tuple[str, ...],
    ) -> None:
        routing = proposal.planner_routing
        assert routing is not None
        profile = routing.assessment.semantic_profile
        assert profile is not None
        deterministic = assess_task(
            goal.statement,
            run_id=proposal.run_id,
            risk=self.routing_risk_floor,
            required_capabilities=capabilities,
        )
        expected_assessment = merge_semantic_profile(deterministic, profile)
        configured = {item.id: item for item in self.strategies}
        assessor = configured.get(routing.assessment_strategy.id)
        candidate_ids = set(routing.candidate_strategy_ids)
        candidates = tuple(
            sorted(
                (item for item in self.strategies if item.id in candidate_ids),
                key=lambda item: item.id,
            )
        )
        allowed_ids = set(self.allowed_strategy_ids)
        allowed_backends = set(self.allowed_backends)
        required = set(expected_assessment.required_capabilities)
        eligible = tuple(
            item
            for item in candidates
            if item.id in allowed_ids
            and item.backend in allowed_backends
            and (item.backend not in {"ollama", "ollama_cli"} or self.local_backend_allowed)
            and required <= set(item.capabilities)
            and expected_assessment.risk <= item.max_risk
            and item.min_complexity <= expected_assessment.complexity <= item.max_complexity
            and item.min_scale <= expected_assessment.scale <= item.max_scale
        )
        if (
            routing.strategy_set != self.strategy_set
            or routing.effective_policy_digest != proposal.effective_policy_digest
            or routing.harness_digest != proposal.harness_digest
            or routing.operator_config_digest != self.operator_config_digest
            or routing.assessment != expected_assessment
            or assessor != routing.assessment_strategy
            or routing.assessment_strategy.id not in allowed_ids
            or routing.assessment_strategy.backend not in allowed_backends
            or (
                routing.assessment_strategy.backend in {"ollama", "ollama_cli"}
                and not self.local_backend_allowed
            )
            or tuple(item.id for item in candidates) != routing.candidate_strategy_ids
            or tuple(item.id for item in eligible) != routing.eligible_strategy_ids
        ):
            raise ValueError("Planner routing decision is stale or mismatched")
        selected = select_strategy(
            eligible,
            mode=routing.selection_mode,
            fixed_strategy_id=(
                routing.selected_strategy.id
                if routing.selection_mode is RoutingMode.FIXED
                else None
            ),
            assessment=expected_assessment,
            allowed_strategy_ids=routing.eligible_strategy_ids,
            allowed_backends=tuple(dict.fromkeys(item.backend for item in eligible)),
            local_backend_allowed=self.local_backend_allowed,
        )
        if selected != routing.selected_strategy or selected.model_copy(
            update={"routing_reasons": ()}
        ) != proposal.planner_strategy.model_copy(update={"routing_reasons": ()}):
            raise ValueError("Planner routing selection is not deterministic")

    def _deterministic_node_assessment(self, run_id: str, node: Node) -> TaskAssessment:
        return assess_task(
            node.objective or node.name,
            run_id=run_id,
            risk=max(self.routing_risk_floor, node.risk),
            required_capabilities=node.required_capabilities,
        )

    def _routing_facts(
        self,
        node: Node,
        dependency_ids: tuple[str, ...],
        deterministic: TaskAssessment,
        effective_policy_digest: str,
        harness_digest: str,
    ) -> NodeRoutingFacts:
        return NodeRoutingFacts(
            risk=deterministic.risk,
            required_capabilities=deterministic.required_capabilities,
            dependency_ids=dependency_ids,
            completion_criterion_ids=tuple(item.id for item in node.completion_criteria),
            context_character_count=deterministic.context_character_count or 0,
            effective_policy_digest=effective_policy_digest,
            harness_digest=harness_digest,
        )

    def _prepare_node_assessments(
        self,
        run_id: str,
        nodes: Mapping[str, Node],
        predecessors: Mapping[str, tuple[str, ...]],
        graph_digest: str,
        effective_policy_digest: str,
        harness_digest: str,
        *,
        invoke: bool,
    ) -> None:
        if invoke and self.node_assessor is None:
            raise ValueError("independent semantic node assessor is unavailable")
        for node_id in sorted(nodes):
            node = nodes[node_id]
            deterministic = self._deterministic_node_assessment(run_id, node)
            facts = self._routing_facts(
                node,
                predecessors[node_id],
                deterministic,
                effective_policy_digest,
                harness_digest,
            )
            existing = self._matching_node_assessments(run_id, node_id, graph_digest)
            if not invoke:
                self._validate_node_assessment(existing, node, facts)
                continue
            if existing:
                raise ValueError("semantic node assessment is duplicated or stale")
            assert self.node_assessor is not None
            profile = self.node_assessor.assess(node.objective or node.name, deterministic)
            assessment = merge_semantic_profile(deterministic, profile)
            record = NodeSemanticAssessmentRecord(
                id=identifier("node-semantic-assessment"),
                run_id=run_id,
                created_at=now(),
                node_id=node.id,
                accepted_graph_revision_digest=graph_digest,
                node_subject_digest=_node_assessment_subject(node),
                planner_hints=_planner_node_hints(node),
                routing_facts=facts,
                assessment_strategy=self.node_assessor.strategy,
                semantic_profile=profile,
                assessment=assessment,
            )
            self.store.put("node_semantic_assessment_v2", record, run_id=run_id)

    def _matching_node_assessments(
        self, run_id: str, node_id: str, graph_digest: str
    ) -> tuple[NodeSemanticAssessmentRecord, ...]:
        return tuple(
            item
            for item in self.store.list_records(
                "node_semantic_assessment_v2", NodeSemanticAssessmentRecord, run_id=run_id
            )
            if item.node_id == node_id and item.accepted_graph_revision_digest == graph_digest
        )

    def _validate_node_assessment(
        self,
        records: tuple[NodeSemanticAssessmentRecord, ...],
        node: Node,
        facts: NodeRoutingFacts,
    ) -> NodeSemanticAssessmentRecord:
        if len(records) != 1:
            raise ValueError("authoritative semantic node assessment is missing or ambiguous")
        record = records[0]
        configured = {item.id: item for item in self.strategies}
        if (
            record.node_subject_digest != _node_assessment_subject(node)
            or record.planner_hints != _planner_node_hints(node)
            or record.routing_facts != facts
            or configured.get(record.assessment_strategy.id) != record.assessment_strategy
        ):
            raise ValueError("authoritative semantic node assessment is stale or mismatched")
        return record

    def _route(
        self,
        run_id: str,
        node: Node,
        dependency_ids: tuple[str, ...],
        graph_digest: str,
        effective_policy_digest: str,
        harness_digest: str,
        *,
        independent_node_assessment: bool,
    ) -> NodeRouteRecord:
        deterministic = self._deterministic_node_assessment(run_id, node)
        facts = self._routing_facts(
            node,
            dependency_ids,
            deterministic,
            effective_policy_digest,
            harness_digest,
        )
        semantic_record: NodeSemanticAssessmentRecord | None = None
        if independent_node_assessment:
            semantic_record = self._validate_node_assessment(
                self._matching_node_assessments(run_id, node.id, graph_digest),
                node,
                facts,
            )
            assessment = semantic_record.assessment
        else:
            assessment = deterministic.model_copy(
                update={
                    "complexity": node.complexity,
                    "scale": node.scale,
                    "semantic_profile": node.semantic_profile,
                }
            )
        allowed_ids = set(self.allowed_strategy_ids)
        allowed_backends = set(self.allowed_backends)
        required = set(facts.required_capabilities)
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
            required_capabilities=facts.required_capabilities,
            strategy_capabilities={item.id: item.capabilities for item in eligible},
            fixed_strategy_id=self.fixed_strategy_id,
            assessment=assessment,
            allowed_strategy_ids=tuple(item.id for item in eligible),
            allowed_backends=tuple(dict.fromkeys(item.backend for item in eligible)),
            local_backend_allowed=self.local_backend_allowed,
        )
        selected = selected.model_copy(
            update={
                "routing_reasons": (
                    *selected.routing_reasons,
                    "policy, Harness, risk, capability, dependency, completion, "
                    "and context facts preserved",
                    (
                        "independent semantic node assessment selected compatibility bands"
                        if semantic_record is not None
                        else (
                            "compatible deterministic node routing used without semantic "
                            "reassessment"
                        )
                    ),
                )
            }
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
            planner_hints=None if semantic_record is None else semantic_record.planner_hints,
            routing_facts=facts,
            semantic_assessment_digest=(
                None if semantic_record is None else semantic_record.content_digest
            ),
            eligible_strategy_ids=tuple(item.id for item in eligible),
            selected_strategy=selected,
            effective_policy_digest=effective_policy_digest,
            harness_digest=harness_digest,
        )

    def _task_review_repair_goal(self, node: Node, feedback: tuple[Digest, ...]) -> str:
        if len(feedback) != 3:
            raise ValueError("task-review repair feedback must bind request, result, and decision")
        results = self.store.list_records("task_review_result_v2", TaskReviewResult)
        decisions = self.store.list_records("task_review_decision_v2", TaskReviewDecision)
        result = next((item for item in results if item.content_digest == feedback[1]), None)
        decision = next((item for item in decisions if item.content_digest == feedback[2]), None)
        if (
            result is None
            or decision is None
            or decision.action is not TaskReviewAction.REPAIR
            or decision.result_digest != result.content_digest
            or decision.node_id != node.id
        ):
            raise ValueError("task-review repair feedback is stale or non-authoritative")
        accepted = tuple(
            item
            for item in result.findings
            if canonical_digest(item) in decision.accepted_finding_digests
        )
        if len(accepted) != len(decision.accepted_finding_digests):
            raise ValueError("task-review repair findings do not match the accepted digests")
        lines = [node.objective or node.name, "", "Accepted semantic review repair objectives:"]
        for finding in accepted:
            refs = ",".join((*finding.evidence_digests, *finding.artifact_digests))
            lines.append(
                f"- {finding.id} [{canonical_digest(finding)}] {finding.repair_objective} "
                f"(evidence: {refs})"
            )
        value = "\n".join(lines)
        if len(value) > 20_000:
            raise ValueError("accepted task-review repair context exceeds the worker bound")
        return value

    def _node_evaluation_repair_goal(self, node: Node, feedback: tuple[Digest, ...]) -> str:
        """Render compact exact verifier feedback without exposing artifact bodies."""

        lines = [node.objective or node.name, "", "Accepted deterministic repair evidence:"]
        verification_digests = set(feedback[2:])
        execution_results = (
            *self.store.list_records("verification_result_v2", ExecutionResult),
            *self.store.list_records("action_result_v2", ExecutionResult),
        )
        matched = tuple(
            result for result in execution_results if result.content_digest in verification_digests
        )
        for result in matched:
            failure = result.failure
            code = "VERIFICATION_FAILED" if failure is None else failure.code.value
            message = "required Harness command failed" if failure is None else failure.message
            artifact_refs = tuple(
                digest
                for digest in (result.stdout_artifact_digest, result.stderr_artifact_digest)
                if digest is not None
            )
            lines.append(
                f"- {code}: {message} (result: {result.content_digest}; "
                f"output artifacts: {','.join(artifact_refs) or 'none'})"
            )
        diagnostics = tuple(
            item
            for item in self.store.list_records(
                "worker_boundary_diagnostic_v2", WorkerBoundaryDiagnostic
            )
            if item.content_digest in verification_digests
        )
        for diagnostic in diagnostics:
            lines.append(
                f"- {diagnostic.code}: {diagnostic.exception_message} "
                f"(stage: {diagnostic.stage}; diagnostic: {diagnostic.content_digest})"
            )
        if not matched and not diagnostics:
            lines.append(f"- NODE_EVALUATION_NOT_PASS (evidence: {','.join(feedback)})")
        lines.append(
            "Repair only this node's bounded objective, then return a complete replacement patch."
        )
        value = "\n".join(lines)
        if len(value) > 20_000:
            raise ValueError("accepted node repair context exceeds the worker bound")
        return value

    def _parent_evaluation_repair_goal(self, node: Node, feedback: tuple[Digest, ...]) -> str:
        from .graph_evaluation import ParentCandidateEvaluationRecord

        evaluations = self.store.list_records(
            "parent_candidate_evaluation_v2", ParentCandidateEvaluationRecord
        )
        evaluation = next(
            (item for item in evaluations if item.content_digest == feedback[0]), None
        )
        if evaluation is None or evaluation.status != "failed":
            raise ValueError("parent repair feedback is stale or non-failing")
        result_digests = set(evaluation.verification_result_digests)
        results = tuple(
            item
            for item in self.store.list_records("verification_result_v2", ExecutionResult)
            if item.content_digest in result_digests
        )
        lines = [node.objective or node.name, "", "Accepted parent-candidate repair evidence:"]
        for result in results:
            if result.status == "succeeded":
                continue
            failure = result.failure
            code = "VERIFICATION_FAILED" if failure is None else failure.code.value
            message = "parent Harness verification failed" if failure is None else failure.message
            lines.append(f"- {code}: {message} (result: {result.content_digest})")
        if len(lines) == 3:
            lines.append(
                f"- {evaluation.failure_code or 'PARENT_EVALUATION_FAILED'} "
                f"(evaluation: {evaluation.content_digest})"
            )
        lines.append(
            "Repair the complete single-node candidate against this accepted parent evidence."
        )
        value = "\n".join(lines)
        if len(value) > 20_000:
            raise ValueError("accepted parent repair context exceeds the worker bound")
        return value

    def _task_review_pass_is_authoritative(self, record: NodeExecutionRecord) -> bool:
        result_generation = (
            record.generation if record.output_generation is None else record.output_generation
        )
        requests = self.store.list_records(
            "task_review_request_v2", TaskReviewRequest, run_id=record.run_id
        )
        results = self.store.list_records(
            "task_review_result_v2", TaskReviewResult, run_id=record.run_id
        )
        decisions = self.store.list_records(
            "task_review_decision_v2", TaskReviewDecision, run_id=record.run_id
        )
        candidates = tuple(
            item
            for item in decisions
            if item.node_id == record.node_id
            and item.accepted_graph_revision_digest == record.accepted_graph_revision_digest
            and item.generation == result_generation
            and item.attempt == record.attempt
            and item.action is TaskReviewAction.PASS
        )
        if not candidates:
            return False
        decision = max(candidates, key=lambda item: (item.created_at, item.id))
        request = next(
            (item for item in requests if item.content_digest == decision.request_digest), None
        )
        result = next(
            (item for item in results if item.content_digest == decision.result_digest), None
        )
        if request is None or result is None:
            return False
        try:
            validate_task_review_result(request, result)
        except ValueError:
            return False
        return bool(
            request.node_id == record.node_id
            and request.worker_request_digest == record.worker_request_digest
            and request.worker_result_digest == record.worker_result_digest
            and record.evidence_digest in request.deterministic_evidence_digests
            and record.evaluator_digest in request.deterministic_evidence_digests
            and decision.result_digest == result.content_digest
        )

    def _review_verified_result(
        self,
        graph_run: GraphRunRecord,
        node: Node,
        worker_request: WorkerRequest,
        execution_result: NodeExecutionResult,
        node_record: NodeExecutionRecord,
        graph_digest: Digest,
    ) -> tuple[TaskReviewDecision, tuple[Digest, ...]]:
        """Persist one exact review chain and return its authoritative feedback digests."""

        assert self.task_reviewer is not None
        if (
            node_record.evaluator_decision is not EvaluationDecision.PASS
            or node_record.evidence_digest is None
            or node_record.evaluator_digest is None
            or node_record.worker_result_digest is None
            or worker_request.content_digest is None
            or execution_result.worker_result.content_digest is None
        ):
            raise ValueError("task review requires an exact successful verification chain")
        criterion_by_id = {item.criterion_id: item for item in execution_result.criterion_evidence}
        criterion_ids = tuple(item.id for item in node.completion_criteria)
        if (
            not criterion_ids
            or len(criterion_ids) > 16
            or set(criterion_by_id) != set(criterion_ids)
        ):
            raise ValueError("independent task review supports one to sixteen exact criteria")
        deterministic_digests = tuple(
            dict.fromkeys(
                (
                    node_record.evidence_digest,
                    node_record.evaluator_digest,
                    *node_record.verification_result_digests,
                )
            )
        )
        review_request = TaskReviewRequest(
            id=identifier("task-review-request"),
            run_id=graph_run.id,
            created_at=now(),
            node_id=node.id,
            objective=node.objective or node.name,
            completion_criteria=tuple(item.description for item in node.completion_criteria),
            criterion_ids=criterion_ids,
            accepted_graph_revision_digest=graph_digest,
            generation=node.generation,
            attempt=node.attempt,
            reviewer_strategy=self.task_reviewer.strategy,
            harness_digest=graph_run.harness_digest,
            effective_policy_digest=graph_run.effective_policy_digest,
            worker_request_digest=worker_request.content_digest,
            worker_request=worker_request,
            worker_result_digest=execution_result.worker_result.content_digest,
            worker_result=execution_result.worker_result,
            criterion_evidence=tuple(criterion_by_id[item] for item in criterion_ids),
            deterministic_evidence_digests=deterministic_digests,
            artifact_descriptors=execution_result.artifact_descriptors,
        )
        self.store.put("task_review_request_v2", review_request, run_id=graph_run.id)
        review_result: TaskReviewResult | None = None
        try:
            review_result = self.task_reviewer.review(review_request)
            validate_task_review_result(review_request, review_result)
            self.store.put("task_review_result_v2", review_result, run_id=graph_run.id)
            decision = decide_task_review(
                review_request,
                review_result,
                block_severities=self.task_review_block_severities,
                decision_id=identifier("task-review-decision"),
                run_id=graph_run.id,
                created_at=now(),
            )
        except Exception:
            if review_result is not None:
                self.store.put(
                    "stale_task_review_result_v2",
                    StaleTaskReviewResult(
                        id=identifier("stale-task-review-result"),
                        run_id=graph_run.id,
                        created_at=now(),
                        node_id=node.id,
                        request_digest=_required_digest(review_request.content_digest),
                        result_digest=_required_digest(review_result.content_digest),
                        expected_graph_revision_digest=graph_digest,
                        result_graph_revision_digest=review_result.accepted_graph_revision_digest,
                        expected_generation=node.generation,
                        result_generation=review_result.generation,
                        expected_attempt=node.attempt,
                        result_attempt=review_result.attempt,
                    ),
                    run_id=graph_run.id,
                )
            decision = TaskReviewDecision(
                id=identifier("task-review-decision"),
                run_id=graph_run.id,
                created_at=now(),
                request_digest=_required_digest(review_request.content_digest),
                node_id=node.id,
                accepted_graph_revision_digest=graph_digest,
                generation=node.generation,
                attempt=node.attempt,
                action=TaskReviewAction.FAIL,
                reason_code="TASK_REVIEW_FAILED",
            )
        self.store.put("task_review_decision_v2", decision, run_id=graph_run.id)
        feedback = tuple(
            item
            for item in (
                review_request.content_digest,
                None if review_result is None else review_result.content_digest,
                decision.content_digest,
            )
            if item is not None
        )
        return decision, feedback

    def _persist_result(
        self,
        node: Node,
        request: WorkerRequest,
        result: NodeExecutionResult,
        records: dict[str, NodeExecutionRecord],
        evidence_by_node: dict[str, NodeEvidenceRecord],
        graph_digest: str,
    ) -> None:
        try:
            persisted_request = self.store.get("worker_request_v2", request.id, WorkerRequest)
        except KeyError:
            raise ValueError("worker request criterion binding is absent or stale") from None
        if persisted_request != request or request.completion_criteria != node.completion_criteria:
            raise ValueError("worker request criterion binding is absent or stale")

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
                if worker_result.boundary_diagnostic is not None:
                    self.store.put(
                        "worker_boundary_diagnostic_v2",
                        worker_result.boundary_diagnostic,
                        run_id=request.graph_run_id,
                    )
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
        if (
            self.bounded_graph_execution
            and worker_result.status == "succeeded"
            and result.failure_code is None
        ):
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
        if worker_result.boundary_diagnostic is not None:
            self.store.put(
                "worker_boundary_diagnostic_v2",
                worker_result.boundary_diagnostic,
                run_id=request.graph_run_id or request.run_id,
            )
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
                None
                if decision is EvaluationDecision.PASS
                else (
                    result.failure_code
                    or (
                        worker_result.failure.code.value
                        if worker_result.failure is not None
                        else "NODE_EVALUATION_NOT_PASS"
                    )
                )
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
                or record.evidence_id is None
                or record.evidence_digest is None
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
            evidence = self.store.get("node_evidence_v2", record.evidence_id, NodeEvidenceRecord)
            evidence_revision = record.retained_from_revision_digest or graph_digest
            if (
                worker_result.content_digest != record.worker_result_digest
                or evidence.content_digest != record.evidence_digest
                or evidence.node_id != record.node_id
                or evidence.generation != record.output_generation
                or evidence.attempt != record.attempt
                or evidence.accepted_graph_revision_digest != evidence_revision
                or evaluator.content_digest != record.evaluator_digest
                or evaluator.node_id != record.node_id
                or evaluator.generation != record.output_generation
                or evaluator.attempt != record.attempt
                or evaluator.accepted_graph_revision_digest != evidence_revision
                or evaluator.worker_result_digest != record.worker_result_digest
                or evaluator.evidence_digest != record.evidence_digest
                or evaluator.decision is not EvaluationDecision.PASS
            ):
                raise ValueError("predecessor result, evidence, or evaluator binding is stale")
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

    def _predecessor_context(
        self,
        predecessors: tuple[NodeExecutionRecord, ...],
        graph_digest: Digest,
        generation: int,
    ) -> (
        tuple[
            tuple[PredecessorOutputReference, ...],
            tuple[Digest, ...],
        ]
        | None
    ):
        try:
            outputs = self._predecessor_outputs(predecessors, graph_digest, generation)
        except (KeyError, ValueError):
            return None
        return (
            outputs,
            tuple(_required_digest(record.evidence_digest) for record in predecessors),
        )

    def _pre_dispatch_bindings_are_valid(
        self,
        graph_run: GraphRunRecord,
        node: Node,
        request: WorkerRequest,
        route: NodeRouteRecord,
        dependency_ids: tuple[str, ...],
    ) -> bool:
        try:
            persisted_run = self.store.get("graph_run_v2", graph_run.id, GraphRunRecord)
            persisted_request = self.store.get("worker_request_v2", request.id, WorkerRequest)
            persisted_route = self.store.get("node_route_v2", route.id, NodeRouteRecord)
        except (KeyError, ValueError):
            return False

        expected_facts = self._routing_facts(
            node,
            dependency_ids,
            self._deterministic_node_assessment(graph_run.id, node),
            graph_run.effective_policy_digest,
            graph_run.harness_digest,
        )
        required = set(expected_facts.required_capabilities)
        available = set(graph_run.available_capabilities) - set(
            graph_run.execution_policy.denied_capabilities
        )
        return not (
            persisted_run != graph_run
            or persisted_request != request
            or persisted_route != route
            or request.graph_run_id != graph_run.id
            or request.node_id != node.id
            or request.accepted_plan_digest != graph_run.accepted_graph_revision_digest
            or request.accepted_graph_revision_digest != graph_run.accepted_graph_revision_digest
            or request.generation != node.generation
            or request.attempt != node.attempt
            or request.task_kind != graph_run.goal.task_kind
            or request.processes_authorized != graph_run.goal.processes_authorized
            or request.harness_digest != graph_run.harness_digest
            or request.effective_policy_digest != graph_run.effective_policy_digest
            or request.required_capabilities != expected_facts.required_capabilities
            or route.run_id != graph_run.id
            or route.node_id != node.id
            or route.accepted_graph_revision_digest != graph_run.accepted_graph_revision_digest
            or route.generation != node.generation
            or route.attempt != node.attempt
            or route.effective_policy_digest != graph_run.effective_policy_digest
            or route.harness_digest != graph_run.harness_digest
            or route.routing_facts != expected_facts
            or route.assessment.run_id != graph_run.id
            or route.assessment.required_capabilities != expected_facts.required_capabilities
            or not required <= set(route.selected_strategy.capabilities)
            or not required <= available
            or (request.task_kind == GoalTaskKind.NON_MUTATING and "edit_intent" in required)
            or (not request.processes_authorized and "process" in required)
        )

    def _worker_context_is_valid(
        self,
        node: Node,
        request: WorkerRequest,
        manifest: WorkerContextManifest,
        graph_digest: Digest,
        graph_run_id: Identifier,
    ) -> bool:
        try:
            persisted = self.store.get(
                "worker_context_manifest_v2", manifest.id, WorkerContextManifest
            )
        except KeyError:
            return False
        return (
            persisted == manifest
            and request.content_digest == manifest.worker_request_digest
            and request.id == manifest.worker_request_id
            and request.run_id == manifest.worker_run_id
            and request.graph_run_id == graph_run_id == manifest.run_id
            and request.node_id == node.id == manifest.node_id
            and canonical_digest(request.goal) == manifest.objective_digest
            and request.task_kind == manifest.task_kind
            and request.processes_authorized == manifest.processes_authorized
            and canonical_digest(request.completion_criteria) == manifest.completion_criteria_digest
            and request.completion_criteria == node.completion_criteria
            and request.required_capabilities
            == node.required_capabilities
            == manifest.required_capabilities
            and request.accepted_plan_digest == graph_digest
            and request.accepted_graph_revision_digest
            == graph_digest
            == manifest.accepted_graph_revision_digest
            and request.generation == node.generation == manifest.generation
            and request.attempt == node.attempt == manifest.attempt
            and request.workspace_context == manifest.workspace_context
            and request.harness_digest == manifest.harness_digest
            and request.effective_policy_digest == manifest.effective_policy_digest
            and request.remaining_budgets == manifest.remaining_budgets
            and tuple(item.node_id for item in request.predecessor_outputs)
            == manifest.predecessor_node_ids
            and request.prior_result_digests == manifest.predecessor_result_digests
            and request.accepted_feedback_digests == manifest.accepted_feedback_digests
            and self._repair_feedback_is_authoritative(node, request)
            and tuple(
                artifact
                for item in request.predecessor_outputs
                for artifact in item.artifact_descriptors
            )
            == manifest.artifact_descriptors
            and not manifest.conversation_history_included
            and not manifest.artifact_bodies_included
        )

    def _repair_feedback_is_authoritative(
        self,
        node: Node,
        request: WorkerRequest,
    ) -> bool:
        feedback = request.accepted_feedback_digests
        if request.graph_run_id is None:
            return not feedback
        repair = self._active_repair_transition(
            request.graph_run_id,
            node.id,
            request.accepted_graph_revision_digest,
            request.generation,
            request.attempt,
        )
        if repair is None:
            return not feedback
        if feedback != repair.evidence_digests:
            return False
        if repair.reason_code == "ACCEPTED_TASK_REVIEW_FEEDBACK":
            if len(feedback) != 3:
                return False
            review_requests = self.store.list_records(
                "task_review_request_v2", TaskReviewRequest, run_id=request.graph_run_id
            )
            review_results = self.store.list_records(
                "task_review_result_v2", TaskReviewResult, run_id=request.graph_run_id
            )
            review_decisions = self.store.list_records(
                "task_review_decision_v2", TaskReviewDecision, run_id=request.graph_run_id
            )
            trusted_request = next(
                (item for item in review_requests if item.content_digest == feedback[0]), None
            )
            trusted_result = next(
                (item for item in review_results if item.content_digest == feedback[1]), None
            )
            trusted_decision = next(
                (item for item in review_decisions if item.content_digest == feedback[2]), None
            )
            if (
                trusted_request is None
                or trusted_result is None
                or trusted_decision is None
                or trusted_result.request_digest != trusted_request.content_digest
                or trusted_decision.request_digest != trusted_request.content_digest
                or trusted_decision.result_digest != trusted_result.content_digest
                or trusted_decision.action is not TaskReviewAction.REPAIR
                or trusted_request.node_id != node.id
                or trusted_request.accepted_graph_revision_digest
                != repair.accepted_graph_revision_digest
                or trusted_request.generation != repair.generation
                or trusted_request.attempt != repair.attempt
                or trusted_request.worker_request_digest != repair.worker_request_digest
                or trusted_request.worker_result_digest != repair.worker_result_digest
                or trusted_result.node_id != node.id
                or trusted_result.accepted_graph_revision_digest
                != repair.accepted_graph_revision_digest
                or trusted_result.generation != repair.generation
                or trusted_result.attempt != repair.attempt
            ):
                return False
            try:
                validate_task_review_result(trusted_request, trusted_result)
            except ValueError:
                return False
            return True
        if repair.reason_code == "ACCEPTED_PARENT_EVALUATION_FEEDBACK":
            from .graph_evaluation import ParentCandidateEvaluationRecord

            evaluations = self.store.list_records(
                "parent_candidate_evaluation_v2",
                ParentCandidateEvaluationRecord,
                run_id=request.graph_run_id,
            )
            evaluation = next(
                (item for item in evaluations if item.content_digest == feedback[0]), None
            )
            if evaluation is None or evaluation.status != "failed":
                return False
            expected = tuple(
                dict.fromkeys(
                    (
                        evaluation.content_digest,
                        evaluation.request_digest,
                        *evaluation.verification_result_digests,
                        *evaluation.evaluation_ledger_digests,
                    )
                )
            )
            return bool(
                feedback == expected
                and evaluation.accepted_graph_revision_digest
                == repair.accepted_graph_revision_digest
            )
        if len(feedback) < 2 or repair.reason_code not in {
            "ACCEPTED_NODE_EVALUATION_FEEDBACK",
            _PROTOCOL_PREFLIGHT_CORRECTION_REASON,
        }:
            return False
        history = self.store.list_records(
            "node_execution_v2", NodeExecutionRecord, run_id=request.graph_run_id
        )
        candidates = tuple(
            item
            for item in history
            if item.node_id == node.id
            and item.accepted_graph_revision_digest == repair.accepted_graph_revision_digest
            and item.generation == repair.generation
            and item.attempt == repair.attempt
            and item.status == "failed"
            and item.evaluator_decision is EvaluationDecision.FAIL
            and item.worker_request_digest is not None
            and item.worker_result_id is not None
            and item.worker_result_digest is not None
            and item.evidence_id is not None
            and item.evidence_digest is not None
            and item.evaluator_id is not None
            and item.evaluator_digest is not None
        )
        if not candidates:
            return False
        prior = max(candidates, key=lambda item: (item.sequence, item.created_at, item.id))
        if (
            repair.worker_request_digest != prior.worker_request_digest
            or repair.worker_result_digest != prior.worker_result_digest
        ):
            return False
        assert prior.worker_result_id is not None
        assert prior.evidence_id is not None
        assert prior.evaluator_id is not None
        try:
            worker_result = self.store.get("worker_result_v2", prior.worker_result_id, WorkerResult)
            evidence = self.store.get("node_evidence_v2", prior.evidence_id, NodeEvidenceRecord)
            evaluator = self.store.get("node_evaluator_v2", prior.evaluator_id, NodeEvaluatorRecord)
        except KeyError:
            return False
        failed_actions = tuple(
            item.content_digest
            for item in self.store.list_records(
                "action_result_v2", ExecutionResult, run_id=worker_result.run_id
            )
            if item.status != "succeeded" and item.content_digest is not None
        )
        boundary_feedback = (
            ()
            if worker_result.boundary_diagnostic is None
            else (_required_digest(worker_result.boundary_diagnostic.content_digest),)
        )
        if repair.evidence_digests != (
            prior.evidence_digest,
            prior.evaluator_digest,
            *prior.verification_result_digests,
            *failed_actions,
            *boundary_feedback,
        ):
            return False
        verification_results = self.store.list_records(
            "verification_result_v2", ExecutionResult, run_id=worker_result.run_id
        )
        if (
            tuple(
                item.content_digest
                for item in verification_results
                if item.content_digest in prior.verification_result_digests
            )
            != prior.verification_result_digests
        ):
            return False
        return bool(
            worker_result.content_digest == prior.worker_result_digest
            and worker_result.request_digest == prior.worker_request_digest
            and evidence.content_digest == prior.evidence_digest
            and evidence.node_id == node.id
            and evidence.accepted_graph_revision_digest == repair.accepted_graph_revision_digest
            and evidence.generation == repair.generation
            and evidence.attempt == repair.attempt
            and evaluator.content_digest == prior.evaluator_digest
            and evaluator.node_id == node.id
            and evaluator.accepted_graph_revision_digest == repair.accepted_graph_revision_digest
            and evaluator.generation == repair.generation
            and evaluator.attempt == repair.attempt
            and evaluator.worker_result_digest == prior.worker_result_digest
            and evaluator.evidence_digest == prior.evidence_digest
            and evaluator.decision is EvaluationDecision.FAIL
        )

    def _active_repair_transition(
        self,
        run_id: Identifier,
        node_id: Identifier,
        graph_digest: Digest | None,
        generation: int,
        attempt: int,
    ) -> LoopTransitionRecord | None:
        if graph_digest is None or attempt < 1:
            return None
        transitions = self.store.list_records(
            "loop_transition_v2", LoopTransitionRecord, run_id=run_id
        )
        candidates = tuple(
            item
            for item in transitions
            if item.action is LoopAction.REPAIR
            and item.node_id == node_id
            and item.accepted_graph_revision_digest == graph_digest
            and item.generation <= generation
            and item.attempt < attempt
        )
        if not candidates:
            return None
        repair = max(
            candidates,
            key=lambda item: (item.generation, item.attempt, item.created_at, item.id),
        )
        repair_order = (repair.generation, repair.attempt, repair.created_at, repair.id)
        if any(
            item.node_id == node_id
            and item.accepted_graph_revision_digest == graph_digest
            and item.action in {LoopAction.PASS, LoopAction.FAIL, LoopAction.ESCALATE}
            and (item.generation, item.attempt, item.created_at, item.id) > repair_order
            for item in transitions
        ):
            return None
        if generation == repair.generation:
            return repair
        resumed = any(
            item.action == "resume" and item.generation == generation
            for item in self.store.list_records(
                "graph_control_fact_v2", GraphControlFact, run_id=run_id
            )
        )
        return repair if resumed else None

    def _observe_worker_attempt(self, request: WorkerRequest) -> WorkerAttemptObservation:
        actions = self.store.list_records(
            "action_result_v2", ExecutionResult, run_id=request.run_id
        )
        artifacts = self.store.list_records(
            "artifact_descriptor_v2", ArtifactDescriptor, run_id=request.run_id
        )
        latest_action = max(actions, key=lambda item: (item.created_at, item.id), default=None)
        latest_artifact = max(artifacts, key=lambda item: (item.created_at, item.id), default=None)
        diffs = tuple(
            item
            for item in artifacts
            if "diff" in item.logical_kind or "patch" in item.logical_kind
        )
        latest_diff = max(diffs, key=lambda item: (item.created_at, item.id), default=None)
        stdout_bytes = stderr_bytes = 0
        for action in actions:
            usage = action.resource_usage
            if isinstance(usage, Mapping):
                stdout = usage.get("stdout_bytes", 0)
                stderr = usage.get("stderr_bytes", 0)
                if isinstance(stdout, int) and not isinstance(stdout, bool):
                    stdout_bytes += max(0, stdout)
                if isinstance(stderr, int) and not isinstance(stderr, bool):
                    stderr_bytes += max(0, stderr)
        return WorkerAttemptObservation(
            process_status="running" if latest_action is None else latest_action.status,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            last_mediated_action_digest=(
                None if latest_action is None else latest_action.content_digest
            ),
            last_artifact_digest=(
                None if latest_artifact is None else latest_artifact.artifact_digest
            ),
            last_diff_digest=None if latest_diff is None else latest_diff.artifact_digest,
        )

    def _advance(self, record: NodeExecutionRecord, **changes: object) -> NodeExecutionRecord:
        payload = record.model_dump(mode="python")
        payload.update(changes)
        payload.update(
            {
                "sequence": record.sequence + 1,
                "transitioned_at": self.clock(),
                "content_digest": None,
            }
        )
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

    def _save_loop_transition(self, transition: LoopTransitionRecord) -> None:
        self.store.put("loop_transition_v2", transition, run_id=transition.run_id)

    def _acquire_run_owner(self, run: GraphRunRecord) -> None:
        acquired_at = ensure_utc(self.clock())
        owner = RunExecutionOwnerRecord(
            id=identifier("run-owner"),
            run_id=run.id,
            created_at=acquired_at,
            graph_run_id=run.id,
            accepted_graph_revision_digest=run.accepted_graph_revision_digest,
            generation=run.generation,
            execution_attempt=run.execution_attempt,
            owner_instance_id=self.owner_instance_id,
            acquired_at=acquired_at,
            last_heartbeat_at=acquired_at,
            expires_at=acquired_at + timedelta(seconds=self.lease_duration_seconds),
            lease_duration_seconds=self.lease_duration_seconds,
        )
        conflict = self.store.acquire_run_owner(owner)
        if conflict is not None:
            with suppress(Exception):
                self.store.put(
                    "run_owner_conflict_v2",
                    RunOwnerConflictRecord(
                        id=identifier("run-owner-conflict"),
                        run_id=run.id,
                        created_at=acquired_at,
                        graph_run_id=run.id,
                        accepted_graph_revision_digest=run.accepted_graph_revision_digest,
                        generation=run.generation,
                        execution_attempt=run.execution_attempt,
                        rejected_owner_instance_id=self.owner_instance_id,
                        current_owner_record_id=str(conflict["owner_record_id"]),
                        current_owner_record_digest=str(conflict["owner_record_digest"]),
                        current_owner_instance_id=str(conflict["owner_instance_id"]),
                        current_generation=cast(int, conflict["generation"]),
                        current_execution_attempt=cast(int, conflict["execution_attempt"]),
                        last_heartbeat_at=ensure_utc(conflict["last_heartbeat_at"]),
                        expires_at=ensure_utc(conflict["expires_at"]),
                    ),
                    run_id=run.id,
                )
            raise RunOwnershipLost("an authoritative owner already exists for this run")
        self._run_owner = owner
        self._next_heartbeat_at = acquired_at + timedelta(seconds=self.heartbeat_interval_seconds)

    def _heartbeat_run_owner_if_due(self, *, force: bool = False) -> None:
        owner = self._run_owner
        if owner is None:
            return
        heartbeat_at = ensure_utc(self.clock())
        if (
            not force
            and self._next_heartbeat_at is not None
            and heartbeat_at < self._next_heartbeat_at
        ):
            return
        current = self.store.current_run_owner(owner.run_id)
        previous_digest = (
            _required_digest(owner.content_digest)
            if current is None
            else str(current["heartbeat_digest"])
        )
        heartbeat = RunLeaseHeartbeatRecord(
            id=identifier("run-heartbeat"),
            run_id=owner.run_id,
            created_at=heartbeat_at,
            graph_run_id=owner.graph_run_id,
            accepted_graph_revision_digest=owner.accepted_graph_revision_digest,
            generation=owner.generation,
            execution_attempt=owner.execution_attempt,
            owner_instance_id=owner.owner_instance_id,
            owner_record_id=owner.id,
            owner_record_digest=_required_digest(owner.content_digest),
            previous_heartbeat_digest=previous_digest,
            heartbeat_at=heartbeat_at,
            expires_at=heartbeat_at + timedelta(seconds=self.lease_duration_seconds),
        )
        if not self.store.heartbeat_run_owner(heartbeat):
            self._record_owner_fence("heartbeat", heartbeat_at)
            raise RunOwnershipLost("run owner lease is expired or superseded")
        self._next_heartbeat_at = heartbeat_at + timedelta(seconds=self.heartbeat_interval_seconds)

    def _assert_run_owner(
        self,
        operation: Literal["heartbeat", "terminalize", "write", "consume_child_result"],
    ) -> None:
        owner = self._run_owner
        if owner is None:
            return
        observed_at = ensure_utc(self.clock())
        current = self.store.current_run_owner(owner.run_id)
        matches = bool(
            current is not None
            and current["status"] == "active"
            and observed_at < ensure_utc(current["expires_at"])
            and current["owner_record_id"] == owner.id
            and current["owner_record_digest"] == owner.content_digest
            and current["graph_revision_digest"] == owner.accepted_graph_revision_digest
            and cast(int, current["generation"]) == owner.generation
            and cast(int, current["execution_attempt"]) == owner.execution_attempt
            and current["owner_instance_id"] == owner.owner_instance_id
        )
        if not matches:
            self._record_owner_fence(operation, observed_at)
            raise RunOwnershipLost("run owner lease is expired or superseded")

    def _record_owner_fence(
        self,
        operation: Literal["heartbeat", "terminalize", "write", "consume_child_result"],
        observed_at: datetime,
    ) -> None:
        owner = self._run_owner
        if owner is None:
            return
        current = self.store.current_run_owner(owner.run_id)
        if current is None:
            reason: Literal["expired", "closed", "missing", "stale", "superseded"] = "missing"
        elif current["status"] != "active":
            reason = "closed"
        elif current["owner_record_id"] != owner.id:
            reason = "superseded"
        elif observed_at >= ensure_utc(current["expires_at"]):
            reason = "expired"
        else:
            reason = "stale"
        with suppress(Exception):
            self.store.put(
                "owner_fence_violation_v2",
                OwnerFenceViolationRecord(
                    id=identifier("owner-fence"),
                    run_id=owner.run_id,
                    created_at=observed_at,
                    graph_run_id=owner.graph_run_id,
                    accepted_graph_revision_digest=owner.accepted_graph_revision_digest,
                    generation=owner.generation,
                    execution_attempt=owner.execution_attempt,
                    owner_instance_id=owner.owner_instance_id,
                    owner_record_id=owner.id,
                    owner_record_digest=_required_digest(owner.content_digest),
                    operation=operation,
                    observed_at=observed_at,
                    reason=reason,
                ),
                run_id=owner.run_id,
            )

    def _terminalize_after_exception(self, status: GraphExecutionStatus, reason: str) -> None:
        owner = self._run_owner
        if owner is None:
            return
        current = self.store.current_run_owner(owner.run_id)
        if current is None or current["status"] != "active":
            return
        self._propagate_owner_interruption(owner)
        with suppress(Exception):
            run = self.store.get("graph_run_v2", owner.run_id, GraphRunRecord)
            if run.status in _OWNED_TERMINAL_GRAPH_STATES:
                return
            terminal = run.model_copy(
                update={
                    "status": status,
                    "failure_code": (
                        "RUN_INTERRUPTED"
                        if status == "interrupted"
                        else f"ORCHESTRATOR_EXCEPTION:{reason}"
                    ),
                }
            )
            self._save_run(terminal)

    def _propagate_owner_interruption(self, owner: RunExecutionOwnerRecord) -> None:
        """Request bounded child cleanup without allowing diagnostics to block closure."""

        requests = self.store.list_records("worker_request_v2", WorkerRequest, run_id=owner.run_id)
        for request in requests:
            if (
                request.graph_run_id != owner.run_id
                or request.accepted_graph_revision_digest != owner.accepted_graph_revision_digest
                or request.generation != owner.generation
            ):
                continue
            propagated = True
            try:
                self.store.request_control(request.run_id, "cancel")
            except (OSError, RuntimeError, ValueError):
                propagated = False
            with suppress(Exception):
                self.store.put(
                    "node_control_propagation_v2",
                    NodeControlPropagationRecord(
                        id=identifier("node-control-propagation"),
                        run_id=owner.run_id,
                        created_at=now(),
                        graph_run_id=owner.run_id,
                        node_id=request.node_id or "unknown-node",
                        child_run_id=request.run_id,
                        accepted_graph_revision_digest=owner.accepted_graph_revision_digest,
                        generation=owner.generation,
                        attempt=request.attempt,
                        propagated=propagated,
                        cleanup_confirmed=False,
                    ),
                    run_id=owner.run_id,
                )

    def _save_run(self, run: GraphRunRecord) -> None:
        owner = self._run_owner
        if owner is None:
            self.store.put("graph_run_v2", run, run_id=run.id, revision=run.generation + 1)
            return
        if (
            run.id != owner.run_id
            or run.accepted_graph_revision_digest != owner.accepted_graph_revision_digest
            or (
                run.generation != owner.generation
                and not (run.status == "cancelled" and run.generation == owner.generation + 1)
            )
            or run.execution_attempt != owner.execution_attempt
        ):
            observed_at = ensure_utc(self.clock())
            self._record_owner_fence("write", observed_at)
            raise RunOwnershipLost("graph state does not match the current owner binding")
        self._heartbeat_run_owner_if_due()
        observed_at = ensure_utc(self.clock())
        if run.status in _OWNED_TERMINAL_GRAPH_STATES:
            closure = self.store.terminalize_owned_graph_run(
                owner,
                run,
                lambda heartbeat_digest: RunLeaseClosureRecord(
                    id=identifier("run-closure"),
                    run_id=run.id,
                    created_at=observed_at,
                    graph_run_id=run.id,
                    accepted_graph_revision_digest=run.accepted_graph_revision_digest,
                    generation=owner.generation,
                    execution_attempt=run.execution_attempt,
                    owner_instance_id=owner.owner_instance_id,
                    owner_record_id=owner.id,
                    owner_record_digest=_required_digest(owner.content_digest),
                    final_heartbeat_digest=heartbeat_digest,
                    closed_at=observed_at,
                    terminal_graph_status=cast(Any, run.status),
                    reason=run.failure_code or run.status,
                ),
                observed_at=observed_at,
            )
            if closure is None:
                self._record_owner_fence("terminalize", observed_at)
                raise RunOwnershipLost("only the authoritative owner can terminalize the run")
            return
        if not self.store.put_owned_graph_run(owner, run, observed_at=observed_at):
            self._record_owner_fence("write", observed_at)
            raise RunOwnershipLost("only the authoritative owner can update the run")


def _worker_request_contract_failure(node: Node, request: WorkerRequest) -> str | None:
    if request.completion_criteria != node.completion_criteria:
        return (
            "worker request completion criteria do not preserve the accepted node contract; "
            "rebuild the request from the persisted graph revision"
        )
    capabilities = set(request.required_capabilities)
    for criterion in request.completion_criteria:
        if isinstance(criterion, str):
            return (
                "worker request completion criteria omit typed evidence requirements; "
                "persist and dispatch the accepted CompletionCriterion records"
            )
        if not criterion.mandatory:
            continue
        patch_required = "workspace_patch" in criterion.required_artifact_ids
        if patch_required and request.task_kind == GoalTaskKind.NON_MUTATING:
            return (
                f"criterion {criterion.id} requires workspace_patch evidence, but the "
                "non-mutating task contract cannot produce a workspace patch; select a "
                "mutating edit_intent contract"
            )
        if patch_required and "edit_intent" not in capabilities:
            return (
                f"criterion {criterion.id} requires workspace_patch evidence, but the node "
                "lacks the edit_intent capability; add it to the accepted node contract"
            )
        if criterion.source == "accepted_non_mutating_result" and (
            request.task_kind != GoalTaskKind.NON_MUTATING
            or criterion.id != f"criterion-{node.id}"
            or criterion.description != "the node-bound worker result is accepted"
            or criterion.required_artifact_ids
        ):
            return (
                f"criterion {criterion.id} selects accepted_non_mutating_result evidence, "
                "but its task/result binding is incompatible with that reserved source; "
                "use the canonical node-bound non-mutating result criterion"
            )
    return None


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


def _node_resources_remain(
    node: Node,
    remaining: Mapping[str, int | float],
) -> bool:
    return (
        int(remaining["node_attempts"]) > 0
        and int(remaining["worker_turns"]) >= node.resource_budget.worker_turns
        and int(remaining["processes"]) >= node.resource_budget.processes
        and float(remaining["wall_seconds"]) >= node.resource_budget.wall_seconds
        and int(remaining["artifact_bytes"]) >= node.resource_budget.artifact_bytes
    )


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


def _plan_review_failure_evidence(
    attempt: PlanReviewAttempt,
    error: BaseException,
) -> PlanReviewFailureEvidence:
    kind = PlanReviewFailureKind.REVIEWER_ERROR
    stdout_artifact_digest = None
    if isinstance(error, PlanReviewInvocationError):
        kind = error.kind
        stdout_artifact_digest = error.stdout_artifact_digest
    return PlanReviewFailureEvidence(
        id=identifier("plan-review-failure"),
        run_id=attempt.run_id,
        created_at=now(),
        plan_review_attempt_id=attempt.id,
        plan_review_attempt_digest=_required_digest(attempt.content_digest),
        failure_kind=kind,
        stdout_artifact_digest=stdout_artifact_digest,
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


def _sanitized_boundary_exception(error: Exception) -> tuple[str, str]:
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", str(error))
    value = re.sub(
        r"(?i)(token|secret|password|credential|api[_-]?key|authorization)"
        r"(\s*[:=]\s*)[^,;\s]+",
        r"\1\2<redacted>",
        value,
    )
    return type(error).__name__[:200], value.strip()[:1_000] or type(error).__name__


def _planner_node_hints(node: Node) -> PlannerNodeRoutingHints:
    return PlannerNodeRoutingHints(
        complexity=node.complexity,
        scale=node.scale,
        risk=node.risk,
        required_capabilities=node.required_capabilities,
        semantic_profile=node.semantic_profile,
    )


def _node_assessment_subject(node: Node) -> str:
    return canonical_digest(
        {
            "node_id": node.id,
            "kind": node.kind,
            "name": node.name,
            "objective": node.objective,
            "output_contract": node.output_contract,
            "completion_criteria": node.completion_criteria,
            "resource_budget": node.resource_budget,
            "retry_limit": node.retry_limit,
            "max_iterations": node.max_iterations,
            "configuration": node.configuration,
            "planner_hints": _planner_node_hints(node),
        }
    )


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
