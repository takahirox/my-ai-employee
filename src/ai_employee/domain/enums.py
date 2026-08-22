"""Stable public enumerations for the foundational domain."""

from .base import StableStrEnum


class RunState(StableStrEnum):
    CREATED = "created"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"


class TaskState(StableStrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class NodeState(StableStrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class FailureKind(StableStrEnum):
    EXECUTION = "execution"
    VALIDATION = "validation"
    POLICY = "policy"
    INVALID_OUTPUT = "invalid_output"
    TIMEOUT = "timeout"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    VERIFICATION = "verification"
    REVIEW = "review"
    GRAPH = "graph"
    CANCELLATION = "cancellation"
    EXTERNAL_BLOCKER = "external_blocker"


class NodeKind(StableStrEnum):
    SYSTEM = "system"
    FUNCTION = "function"
    PREDICATE = "predicate"
    GATE = "gate"
    PROCESS_RESULT = "process_result"


class GateKind(StableStrEnum):
    PREDICATE = "predicate"
    COMMAND_RESULT = "command_result"
    ARTIFACT = "artifact"
    METRIC = "metric"
    APPROVAL = "approval"
    COMPLETION = "completion"


class GateStatus(StableStrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class ResultStatus(StableStrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class Severity(StableStrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class DecisionState(StableStrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    BLOCKED = "blocked"


class MergeDecisionState(StableStrEnum):
    AUTO_MERGE_ELIGIBLE = "auto_merge_eligible"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    MORE_EVIDENCE_REQUIRED = "more_evidence_required"
    CHANGES_REQUIRED = "changes_required"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class RoutingMode(StableStrEnum):
    FIXED = "fixed"
    POLICY = "policy"
    ADAPTIVE = "adaptive"


class MeasurementProvenance(StableStrEnum):
    MEASURED = "measured"
    BACKEND_REPORTED = "backend_reported"
    EXTERNALLY_REPORTED = "externally_reported"
    ESTIMATED = "estimated"
    INFERRED = "inferred"
    UNAVAILABLE = "unavailable"


class ProvenanceKind(StableStrEnum):
    EXPLICIT = "explicit"
    IMPORTED = "imported"
    INFERRED = "inferred"
    RUNTIME_OBSERVED = "runtime_observed"


class ContextRole(StableStrEnum):
    PLANNER = "planner"
    WORKER = "worker"
    VERIFIER = "verifier"
    REVIEWER = "reviewer"


class ContractKind(StableStrEnum):
    OBJECT = "object"
    ARRAY = "array"
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    NULL = "null"
