"""Foundational trust-kernel domain models.

The models intentionally describe stored facts and structured proposals. They do
not grant callers authority to accept a graph, change runtime state, or weaken
policy; those operations live behind deterministic functions.
"""

from __future__ import annotations

import math
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from .base import (
    CanonicalData,
    Digest,
    EntityModel,
    Identifier,
    SchemaModel,
    StableStrEnum,
    UtcTimestamp,
)
from .enums import (
    ContextRole,
    ContractKind,
    DecisionState,
    FailureKind,
    GateKind,
    GateStatus,
    MeasurementProvenance,
    MergeDecisionState,
    NodeKind,
    NodeState,
    ProvenanceKind,
    ResultStatus,
    RoutingMode,
    RunState,
    Severity,
    TaskState,
)


class Reference(SchemaModel):
    kind: Literal["artifact", "evidence", "event", "document", "run", "task", "node"]
    target_id: Identifier
    digest: Digest | None = None
    locator: str | None = Field(default=None, max_length=512)


class Finding(EntityModel):
    code: Identifier
    severity: Severity
    summary: str = Field(min_length=1, max_length=500)
    detail: str = Field(default="", max_length=10_000)
    blocking: bool = False
    references: tuple[Reference, ...] = ()


class Failure(EntityModel):
    kind: FailureKind
    code: Identifier
    message: str = Field(min_length=1, max_length=2_000)
    retryable: bool = False
    details: CanonicalData = None
    references: tuple[Reference, ...] = ()


class Decision(EntityModel):
    state: DecisionState
    rationale: str = Field(min_length=1, max_length=4_000)
    actor: Identifier
    references: tuple[Reference, ...] = ()


class Recommendation(EntityModel):
    summary: str = Field(min_length=1, max_length=500)
    rationale: str = Field(default="", max_length=4_000)
    priority: int = Field(default=0, ge=0, le=100)
    references: tuple[Reference, ...] = ()


class NodeResourceBudget(SchemaModel):
    """Resources one node attempt asks the scheduler to reserve."""

    worker_turns: int = Field(default=1, ge=0)
    processes: int = Field(default=1, ge=0)
    wall_seconds: float = Field(default=1.0, ge=0)
    artifact_bytes: int = Field(default=1_000_000, ge=0)

    @field_validator("wall_seconds")
    @classmethod
    def _finite_wall_seconds(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("resource durations must be finite")
        return value


class Budget(SchemaModel):
    max_attempts: int = Field(default=1, ge=1)
    max_retries: int = Field(default=0, ge=0)
    max_repairs: int = Field(default=0, ge=0)
    max_replans: int = Field(default=0, ge=0)
    max_loop_iterations: int = Field(default=1, ge=1)
    max_nodes: int = Field(default=100, ge=1)
    max_wall_seconds: float = Field(default=3600.0, gt=0)
    max_worker_turns: int = Field(default=100, ge=1)
    max_processes: int = Field(default=100, ge=0)
    max_artifact_bytes: int = Field(default=100_000_000, ge=0)

    @field_validator("max_wall_seconds")
    @classmethod
    def _finite_duration(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("budget durations must be finite")
        return value


class Constraint(EntityModel):
    kind: Identifier
    description: str = Field(min_length=1, max_length=2_000)
    mandatory: bool = True


class CompletionCriterion(EntityModel):
    source: Literal["custom", "accepted_non_mutating_result"] = "custom"
    description: str = Field(min_length=1, max_length=2_000)
    mandatory: bool = True
    verification_requirement_ids: tuple[Identifier, ...] = ()
    required_artifact_ids: tuple[Identifier, ...] = ()


class GoalTaskKind(StableStrEnum):
    """The persisted side-effect contract for an accepted Goal."""

    MUTATING = "mutating"
    NON_MUTATING = "non_mutating"


class Goal(EntityModel):
    statement: str = Field(min_length=1, max_length=10_000)
    task_kind: GoalTaskKind = GoalTaskKind.MUTATING
    processes_authorized: bool = True
    completion_criteria: tuple[CompletionCriterion, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode="after")
    def _task_kind_matches_declared_evidence(self) -> Self:
        required_artifacts = {
            artifact
            for criterion in self.completion_criteria
            for artifact in criterion.required_artifact_ids
        }
        verification_required = any(
            criterion.verification_requirement_ids for criterion in self.completion_criteria
        )
        if self.task_kind is GoalTaskKind.NON_MUTATING and "workspace_patch" in required_artifacts:
            raise ValueError("non-mutating Goal cannot require a workspace_patch")
        if not self.processes_authorized and verification_required:
            raise ValueError("Goal verification requires processes but processes are unauthorized")
        return self


class OutputContract(EntityModel):
    contract_version: str = Field(default="1", pattern=r"^1$")
    expected_type: ContractKind = ContractKind.OBJECT
    required_fields: tuple[Identifier, ...] = ()
    allow_additional_fields: bool = False

    @model_validator(mode="after")
    def _object_fields_only(self) -> Self:
        if self.expected_type is not ContractKind.OBJECT and self.required_fields:
            raise ValueError("required_fields is only valid for object output contracts")
        if len(set(self.required_fields)) != len(self.required_fields):
            raise ValueError("required_fields must be unique")
        return self


class TaskProfile(EntityModel):
    role: ContextRole
    required_capabilities: tuple[Identifier, ...] = ()
    context_policy_id: Identifier | None = None
    output_contract_id: Identifier | None = None


class SemanticTaskType(StableStrEnum):
    MECHANICAL = "mechanical"
    RETRIEVAL = "retrieval"
    DIAGNOSIS = "diagnosis"
    IMPLEMENTATION = "implementation"
    ARCHITECTURE = "architecture"
    RESEARCH = "research"
    PLANNING = "planning"
    OPEN_ENDED_STRATEGY = "open_ended_strategy"


class SemanticReasoningClass(StableStrEnum):
    MECHANICAL = "mechanical"
    SIMPLE = "simple"
    MODERATE = "moderate"
    DEEP = "deep"
    OPEN_ENDED = "open_ended"


class SemanticScope(StableStrEnum):
    BOUNDED = "bounded"
    LOCAL = "local"
    MULTI_COMPONENT = "multi_component"
    BROAD = "broad"


class SemanticAmbiguity(StableStrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SemanticTaskProfile(SchemaModel):
    """Strict semantic facts with no execution or policy authority."""

    task_type: SemanticTaskType
    reasoning_class: SemanticReasoningClass
    scope: SemanticScope
    ambiguity: SemanticAmbiguity
    reasons: tuple[str, ...] = Field(min_length=1, max_length=10)

    @field_validator("reasons")
    @classmethod
    def _bounded_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not reason.strip() or len(reason) > 500 for reason in value):
            raise ValueError("reasons must be non-blank and at most 500 characters")
        return value


class Task(EntityModel):
    title: str = Field(min_length=1, max_length=500)
    profile: TaskProfile
    dependency_ids: tuple[Identifier, ...] = ()
    output_contract: OutputContract | None = None
    state: TaskState = TaskState.PENDING
    generation: int = Field(default=0, ge=0)
    graph_revision: int = Field(default=1, ge=1)
    attempt: int = Field(default=0, ge=0)
    transitions: tuple[StateTransition, ...] = ()
    failure: Failure | None = None


class Plan(EntityModel):
    goal_id: Identifier
    tasks: tuple[Task, ...]
    rationale: str = Field(default="", max_length=10_000)

    @model_validator(mode="after")
    def _unique_tasks(self) -> Self:
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("plan task IDs must be unique")
        return self


class Node(EntityModel):
    kind: NodeKind
    name: str = Field(min_length=1, max_length=500)
    objective: str | None = Field(default=None, min_length=1, max_length=10_000)
    output_contract: OutputContract
    required_capabilities: tuple[Identifier, ...] = ()
    completion_criteria: tuple[CompletionCriterion, ...] = ()
    semantic_profile: SemanticTaskProfile | None = None
    complexity: int = Field(default=1, ge=1, le=10)
    scale: int = Field(default=1, ge=1, le=10)
    risk: int = Field(default=0, ge=0, le=10)
    retry_limit: int = Field(default=0, ge=0)
    resource_budget: NodeResourceBudget = NodeResourceBudget()
    max_iterations: int = Field(default=1, ge=1)
    configuration: CanonicalData = None
    state: NodeState = NodeState.PENDING
    generation: int = Field(default=0, ge=0)
    graph_revision: int = Field(default=1, ge=1)
    attempt: int = Field(default=0, ge=0)
    transitions: tuple[StateTransition, ...] = ()
    failure: Failure | None = None


class Edge(EntityModel):
    source_id: Identifier
    target_id: Identifier
    condition: str | None = Field(default=None, max_length=1_000)
    loop: bool = False
    max_traversals: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _bounded_loop(self) -> Self:
        if self.loop and self.max_traversals is None:
            raise ValueError("loop edges require max_traversals")
        if not self.loop and self.max_traversals is not None:
            raise ValueError("max_traversals is only valid for loop edges")
        return self


class Graph(EntityModel):
    graph_schema_version: str = Field(default="1", pattern=r"^1$")
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...] = ()
    entry_node_ids: tuple[Identifier, ...]
    terminal_node_ids: tuple[Identifier, ...]
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode="after")
    def _local_integrity(self) -> Self:
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("graph node IDs must be unique")
        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("graph edge IDs must be unique")
        known = set(node_ids)
        for edge in self.edges:
            if edge.source_id not in known or edge.target_id not in known:
                raise ValueError(f"edge {edge.id!r} references an unknown node")
        if not self.entry_node_ids or not set(self.entry_node_ids) <= known:
            raise ValueError("entry_node_ids must be non-empty references to graph nodes")
        if not self.terminal_node_ids or not set(self.terminal_node_ids) <= known:
            raise ValueError("terminal_node_ids must be non-empty references to graph nodes")
        return self


class AcceptedGraphRevision(SchemaModel):
    revision_number: int = Field(ge=1)
    graph: Graph
    content_digest: Digest | None = None

    @model_validator(mode="before")
    @classmethod
    def _defensive_graph_snapshot(cls, value: object) -> object:
        """Revalidate and copy candidate content at the acceptance boundary."""

        if not isinstance(value, dict) or "graph" not in value:
            return value
        from ai_employee.serialization import canonical_json

        snapshot = dict(value)
        snapshot["graph"] = Graph.model_validate_json(
            canonical_json(snapshot["graph"]),
            strict=True,
        )
        return snapshot

    @model_validator(mode="after")
    def _bind_digest(self) -> Self:
        from ai_employee.serialization import canonical_digest

        # Preserve the revision-one digest used by existing WorkRun records while
        # making every later accepted revision a distinct execution authority,
        # even when a replan deliberately submits identical graph content.
        actual = (
            canonical_digest(self.graph)
            if self.revision_number == 1
            else canonical_digest({"revision_number": self.revision_number, "graph": self.graph})
        )
        if self.content_digest is not None and self.content_digest != actual:
            raise ValueError("content_digest does not match canonical graph content")
        object.__setattr__(self, "content_digest", actual)
        return self


class ExecutionPolicy(SchemaModel):
    policy_version: str = Field(default="1", pattern=r"^1$")
    denied_capabilities: tuple[Identifier, ...] = ()
    required_approvals: tuple[Identifier, ...] = ()
    max_nodes: int = Field(default=100, ge=1)
    max_attempts: int = Field(default=10, ge=1)
    max_wall_seconds: float = Field(default=3600.0, gt=0)
    network_enabled: bool = False
    unrestricted_process_enabled: bool = False

    @field_validator("max_wall_seconds")
    @classmethod
    def _finite_policy_duration(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("policy durations must be finite")
        return value


class ExecutionStrategy(EntityModel):
    routing_mode: RoutingMode
    backend: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(default="standard", min_length=1, max_length=100)
    context_strategy: str = Field(default="role_default", min_length=1, max_length=100)
    retry_strategy: str = Field(default="bounded", min_length=1, max_length=100)
    escalation_strategy: str = Field(default="deterministic", min_length=1, max_length=100)
    verification_depth: str = Field(default="focused", min_length=1, max_length=100)
    reviewer_strategy: str = Field(default="independent", min_length=1, max_length=100)
    capabilities: tuple[Identifier, ...] = Field(default=(), max_length=100)
    min_complexity: int = Field(default=1, ge=1, le=10)
    max_complexity: int = Field(default=10, ge=1, le=10)
    min_scale: int = Field(default=1, ge=1, le=10)
    max_scale: int = Field(default=10, ge=1, le=10)
    max_risk: int = Field(default=10, ge=0, le=10)
    routing_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid_task_suitability(self) -> Self:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("strategy capabilities must be unique")
        if self.min_complexity > self.max_complexity:
            raise ValueError("minimum complexity cannot exceed maximum complexity")
        if self.min_scale > self.max_scale:
            raise ValueError("minimum scale cannot exceed maximum scale")
        return self


class TaskDecompositionItem(EntityModel):
    """A bounded assessment item, not an executable or recursively nested task."""

    title: str = Field(min_length=1, max_length=500)
    complexity: int = Field(ge=1, le=10)
    scale: int = Field(ge=1, le=10)
    risk: int = Field(ge=0, le=10)
    required_capabilities: tuple[Identifier, ...] = Field(default=(), max_length=50)
    reasons: tuple[str, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _valid_item_assessment(self) -> Self:
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ValueError("required capabilities must be unique")
        if any(not reason.strip() or len(reason) > 1_000 for reason in self.reasons):
            raise ValueError("reasons must be non-blank and at most 1000 characters")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("reasons must be unique and deterministically ordered")
        return self


class SemanticTaskAssessment(SchemaModel):
    """Strict isolated LLM classification; deterministic policy remains authoritative."""

    complexity: int = Field(ge=1, le=10)
    scale: int = Field(ge=1, le=10)
    required_capabilities: tuple[Identifier, ...] = Field(default=(), max_length=20)
    reasons: tuple[str, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _valid_semantic_assessment(self) -> Self:
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ValueError("required capabilities must be unique")
        if any(not reason.strip() or len(reason) > 500 for reason in self.reasons):
            raise ValueError("reasons must be non-blank and at most 500 characters")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("reasons must be unique")
        return self


class TaskAssessment(EntityModel):
    """Persisted bounded assessment used as task-aware routing input."""

    run_id: Identifier
    goal_digest: Digest
    complexity: int = Field(ge=1, le=10)
    scale: int = Field(ge=1, le=10)
    risk: int = Field(ge=0, le=10)
    required_capabilities: tuple[Identifier, ...] = Field(default=(), max_length=100)
    decomposition: tuple[TaskDecompositionItem, ...] = Field(default=(), max_length=100)
    semantic_profile: SemanticTaskProfile | None = None
    context_character_count: int | None = Field(default=None, ge=0, le=10_000)
    reasons: tuple[str, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _valid_assessment(self) -> Self:
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ValueError("required capabilities must be unique")
        if any(not reason.strip() or len(reason) > 1_000 for reason in self.reasons):
            raise ValueError("reasons must be non-blank and at most 1000 characters")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("reasons must be unique and deterministically ordered")
        item_ids = tuple(item.id for item in self.decomposition)
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("decomposition item IDs must be unique")
        return self


class TransitionProvenance(SchemaModel):
    cause: str = Field(min_length=1, max_length=2_000)
    rule_version: str = Field(min_length=1, max_length=100)
    actor: Identifier
    timestamp: UtcTimestamp
    graph_digest: Digest
    policy_digest: Digest
    input_digest: Digest
    evidence_digest: Digest


class StateTransition(SchemaModel):
    entity_kind: Literal["run", "task", "node"]
    entity_id: Identifier
    from_state: RunState | TaskState | NodeState
    to_state: RunState | TaskState | NodeState
    generation: int = Field(ge=0)
    graph_revision: int = Field(ge=1)
    provenance: TransitionProvenance

    @model_validator(mode="after")
    def _state_type_matches_entity(self) -> Self:
        state_type = {
            "run": RunState,
            "task": TaskState,
            "node": NodeState,
        }[self.entity_kind]
        try:
            from_state = state_type(self.from_state.value)
            to_state = state_type(self.to_state.value)
        except ValueError as exc:
            raise ValueError("transition states do not belong to entity_kind") from exc
        if from_state is to_state:
            raise ValueError("a transition must change state")
        object.__setattr__(self, "from_state", from_state)
        object.__setattr__(self, "to_state", to_state)
        return self


class Run(EntityModel):
    goal: Goal
    accepted_graph: AcceptedGraphRevision
    policy: ExecutionPolicy
    state: RunState = RunState.CREATED
    generation: int = Field(default=0, ge=0)
    transitions: tuple[StateTransition, ...] = ()
    failure: Failure | None = None


class Event(EntityModel):
    run_id: Identifier
    event_type: Identifier
    timestamp: UtcTimestamp
    actor: Identifier
    payload: CanonicalData = None


class Artifact(EntityModel):
    run_id: Identifier
    media_type: str = Field(min_length=1, max_length=200)
    digest: Digest
    size_bytes: int = Field(ge=0)
    locator: str = Field(min_length=1, max_length=1_000)
    created_at: UtcTimestamp
    producer_node_id: Identifier | None = None


class Contract(EntityModel):
    name: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=4_000)
    clauses: tuple[str, ...] = ()
    output_contract_ids: tuple[Identifier, ...] = ()


class VerificationRequirement(EntityModel):
    description: str = Field(min_length=1, max_length=2_000)
    mandatory: bool = True
    accepted_evidence_kinds: tuple[Identifier, ...]
    contract_ids: tuple[Identifier, ...] = ()


class VerificationEvidence(EntityModel):
    requirement_ids: tuple[Identifier, ...]
    kind: Identifier
    passed: bool
    summary: str = Field(min_length=1, max_length=2_000)
    artifact_refs: tuple[Reference, ...] = ()
    produced_at: UtcTimestamp
    producer: Identifier


class EvidenceCoverage(SchemaModel):
    requirement_ids: tuple[Identifier, ...]
    satisfied_requirement_ids: tuple[Identifier, ...]
    missing_requirement_ids: tuple[Identifier, ...]
    mapping: CanonicalData
    complete: bool

    @model_validator(mode="after")
    def _consistent_coverage(self) -> Self:
        required = set(self.requirement_ids)
        satisfied = set(self.satisfied_requirement_ids)
        missing = set(self.missing_requirement_ids)
        if satisfied & missing or satisfied | missing != required:
            raise ValueError("satisfied and missing requirements must partition requirement_ids")
        if self.complete != (not missing):
            raise ValueError("complete must agree with missing_requirement_ids")
        return self


class ReviewAssessment(EntityModel):
    reviewer: Identifier
    approved: bool
    blocking_findings: tuple[Finding, ...] = ()
    nonblocking_findings: tuple[Finding, ...] = ()
    summary: str = Field(min_length=1, max_length=4_000)
    assessed_at: UtcTimestamp


class EvidencePack(EntityModel):
    run_id: Identifier
    contract_ids: tuple[Identifier, ...]
    requirements: tuple[VerificationRequirement, ...]
    evidence: tuple[VerificationEvidence, ...]
    coverage: EvidenceCoverage
    reviews: tuple[ReviewAssessment, ...] = ()
    artifact_refs: tuple[Reference, ...] = ()
    created_at: UtcTimestamp


class MergeDecision(EntityModel):
    state: MergeDecisionState
    reasons: tuple[str, ...]
    evidence_pack_id: Identifier
    mandatory_approval_satisfied: bool = False

    @model_validator(mode="after")
    def _eligibility_is_advisory(self) -> Self:
        if self.state is MergeDecisionState.AUTO_MERGE_ELIGIBLE and not self.reasons:
            raise ValueError("auto-merge eligibility requires explicit reasons")
        return self


class ProvenancedValue(SchemaModel):
    value: CanonicalData
    provenance: ProvenanceKind
    source_reference: str | None = Field(default=None, max_length=1_000)
    provisional: bool = False

    @model_validator(mode="after")
    def _inference_is_provisional(self) -> Self:
        if self.provenance is ProvenanceKind.INFERRED and not self.provisional:
            raise ValueError("inferred values must remain provisional")
        return self


class ProjectProfile(EntityModel):
    profile_version: str = Field(default="1", pattern=r"^1$")
    root: str = Field(default=".", pattern=r"^\.(?:/[^/]+)*$")
    commands: CanonicalData = None
    rules: tuple[ProvenancedValue, ...] = ()
    protected_paths: tuple[str, ...] = ()
    generated_paths: tuple[str, ...] = ()
    completion_defaults: CanonicalData = None
    contracts: tuple[Contract, ...] = ()
    verification_requirements: tuple[VerificationRequirement, ...] = ()
    review_rules: tuple[ProvenancedValue, ...] = ()
    canonical_document_refs: tuple[str, ...] = ()
    workspace_preferences: CanonicalData = None


class ContextPolicy(EntityModel):
    role: ContextRole
    max_items: int = Field(default=50, ge=1)
    max_bytes: int = Field(default=64_000, ge=1)
    include_history: bool = False
    allowed_reference_kinds: tuple[str, ...] = (
        "artifact",
        "document",
        "evidence",
    )
    pull_on_demand: bool = True


class ContextPackage(EntityModel):
    run_id: Identifier
    role: ContextRole
    policy_id: Identifier
    compiled_at: UtcTimestamp
    authoritative_refs: tuple[Reference, ...]
    inline_items: CanonicalData = None
    omitted_refs: tuple[Reference, ...] = ()
    source_digest: Digest


class MetricValue(SchemaModel):
    value: float | int | None
    unit: str = Field(min_length=1, max_length=100)
    provenance: MeasurementProvenance
    source: str | None = Field(default=None, max_length=500)

    @field_validator("value")
    @classmethod
    def _finite_metric(cls, value: float | int | None) -> float | int | None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("metric values must be finite")
        return value


class ExecutionMetrics(EntityModel):
    run_id: Identifier
    strategy_id: Identifier
    started_at: UtcTimestamp
    finished_at: UtcTimestamp | None = None
    duration: MetricValue
    input_tokens: MetricValue
    output_tokens: MetricValue
    cost: MetricValue
    attempts: MetricValue
    custom: CanonicalData = None


class StrategyPerformance(EntityModel):
    """Persisted aggregate used by transparent adaptive routing."""

    strategy_id: Identifier
    sample_count: int = Field(default=0, ge=0)
    success_count: int = Field(default=0, ge=0)
    total_duration_seconds: float = Field(default=0.0, ge=0)
    total_cost: float = Field(default=0.0, ge=0)
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def _successes_fit_samples(self) -> Self:
        if self.success_count > self.sample_count:
            raise ValueError("success_count cannot exceed sample_count")
        return self


class ResultEnvelope(SchemaModel):
    contract_id: Identifier
    contract_version: str = Field(default="1", pattern=r"^1$")
    status: ResultStatus
    value: CanonicalData = None
    findings: tuple[Finding, ...] = ()
    failures: tuple[Failure, ...] = ()
    artifact_refs: tuple[Reference, ...] = ()
    evidence_refs: tuple[Reference, ...] = ()
    recommendations: tuple[Recommendation, ...] = ()

    def validate_contract(self, contract: OutputContract) -> None:
        """Validate this envelope's typed value against a versioned output contract."""

        from .base import FrozenDict

        if self.contract_id != contract.id or self.contract_version != contract.contract_version:
            raise ValueError("result envelope references a different output contract")
        type_matches = {
            ContractKind.OBJECT: isinstance(self.value, FrozenDict),
            ContractKind.ARRAY: isinstance(self.value, tuple),
            ContractKind.STRING: isinstance(self.value, str),
            ContractKind.NUMBER: isinstance(self.value, (int, float))
            and not isinstance(self.value, bool),
            ContractKind.BOOLEAN: isinstance(self.value, bool),
            ContractKind.NULL: self.value is None,
        }
        if not type_matches[contract.expected_type]:
            raise ValueError(f"result value is not {contract.expected_type.value}")
        if isinstance(self.value, FrozenDict):
            missing = set(contract.required_fields) - set(self.value)
            if missing:
                raise ValueError(f"result value is missing required fields: {sorted(missing)!r}")
            if not contract.allow_additional_fields:
                extra = set(self.value) - set(contract.required_fields)
                if extra:
                    raise ValueError(f"result value has undeclared fields: {sorted(extra)!r}")


class GateResult(EntityModel):
    kind: GateKind
    status: GateStatus
    summary: str = Field(min_length=1, max_length=2_000)
    observed: CanonicalData = None
    failure: Failure | None = None

    @model_validator(mode="after")
    def _failure_matches_status(self) -> Self:
        if self.status is GateStatus.PASSED and self.failure is not None:
            raise ValueError("a passed gate cannot contain a failure")
        if self.status is not GateStatus.PASSED and self.failure is None:
            raise ValueError("failed and timed-out gates require a structured failure")
        if (
            self.status is GateStatus.TIMEOUT
            and self.failure is not None
            and self.failure.kind is not FailureKind.TIMEOUT
        ):
            raise ValueError("a timed-out gate requires a timeout failure")
        return self
