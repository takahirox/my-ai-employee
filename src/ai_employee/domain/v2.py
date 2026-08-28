"""Strict immutable v2 contracts for mediated Fleet actions and results."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Annotated, ClassVar, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic.main import BaseModel

from ai_employee.serialization import versioned_digest

from .base import CanonicalData, Digest, Identifier, StableStrEnum, UtcTimestamp


class SchemaModelV2(BaseModel):
    """Base for v2 wire contracts without weakening the v1 parser."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        use_enum_values=False,
        arbitrary_types_allowed=True,
    )
    schema_version: Literal["2"] = "2"
    schema_name: ClassVar[str]


class DigestMetadata(SchemaModelV2):
    schema_name: ClassVar[str] = "digest_metadata"
    algorithm: Literal["sha256"] = "sha256"
    format_version: Literal["1"] = "1"


class StableFailureCode(StableStrEnum):
    POLICY_DENIED = "POLICY_DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    INVALID_REQUEST = "INVALID_REQUEST"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    SPAWN_FAILED = "SPAWN_FAILED"
    PROCESS_FAILED = "PROCESS_FAILED"
    NETWORK_BLOCKED = "NETWORK_BLOCKED"
    DNS_REBIND_BLOCKED = "DNS_REBIND_BLOCKED"
    TLS_FAILED = "TLS_FAILED"
    INTEGRITY_FAILED = "INTEGRITY_FAILED"
    INSTALL_DENIED = "INSTALL_DENIED"
    HOST_INSTALL_DENIED = "HOST_INSTALL_DENIED"
    WORKER_UNAVAILABLE = "WORKER_UNAVAILABLE"
    WORKER_PROTOCOL_ERROR = "WORKER_PROTOCOL_ERROR"
    EVALUATOR_EXECUTION_UNAVAILABLE = "EVALUATOR_EXECUTION_UNAVAILABLE"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    REVIEW_BLOCKED = "REVIEW_BLOCKED"
    WORKSPACE_CONFLICT = "WORKSPACE_CONFLICT"
    PROMOTION_FAILED = "PROMOTION_FAILED"
    INDETERMINATE = "INDETERMINATE"


class StableFailure(SchemaModelV2):
    schema_name: ClassVar[str] = "stable_failure"
    code: StableFailureCode
    message: str = Field(min_length=1, max_length=2_000)
    retryable: bool = False
    details: CanonicalData = None


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or value.startswith("/") or "\\" in value or ".." in path.parts:
        raise ValueError("path must be a contained workspace-relative POSIX path")
    if value != "." and path.as_posix() != value:
        raise ValueError("path must use canonical workspace-relative POSIX syntax")
    return value


RelativePath = Annotated[str, Field(max_length=1_000)]


class DigestedRecordV2(SchemaModelV2):
    """Common immutable identity and content binding for public v2 records."""

    id: Identifier
    run_id: Identifier
    created_at: UtcTimestamp
    digest_metadata: DigestMetadata = DigestMetadata()
    content_digest: Digest | None = None

    @model_validator(mode="after")
    def _bind_content_digest(self) -> Self:
        actual = versioned_digest(
            _digest_content(self),
            algorithm=self.digest_metadata.algorithm,
            format_version=self.digest_metadata.format_version,
        )
        if self.content_digest is not None and self.content_digest != actual:
            raise ValueError("content_digest does not match canonical v2 content")
        object.__setattr__(self, "content_digest", actual)
        return self


def _digest_content(value: object) -> object:
    """Strip metadata only from typed records, never from arbitrary payload mappings."""

    if isinstance(value, BaseModel):
        excluded = (
            {"content_digest", "id", "run_id", "created_at"}
            if isinstance(value, DigestedRecordV2)
            else set()
        )
        return {
            name: _digest_content(getattr(value, name))
            for name in type(value).model_fields
            if name not in excluded
        }
    if isinstance(value, Mapping):
        return {key: _digest_content(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_digest_content(item) for item in value]
    return value


class SecretBinding(SchemaModelV2):
    """Opaque secret reference. Raw secret material is deliberately unrepresentable."""

    schema_name: ClassVar[str] = "secret_binding"
    name: Identifier
    binding_ref: Identifier
    domain_scope: tuple[str, ...] = ()


class ProcessRequest(DigestedRecordV2):
    schema_name: ClassVar[str] = "process_request"
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: RelativePath = "."
    environment: tuple[tuple[str, str], ...] = ()
    inherit_environment: tuple[Identifier, ...] = ()
    secret_bindings: tuple[SecretBinding, ...] = ()
    stdin_artifact_digest: Digest | None = None
    timeout_seconds: float = Field(default=300.0, gt=0)
    expected_exit_codes: tuple[int, ...] = (0,)
    stdout_bytes: int = Field(default=1_000_000, ge=1)
    stderr_bytes: int = Field(default=1_000_000, ge=1)
    budget_class: Identifier = "default"
    purpose: str = Field(min_length=1, max_length=1_000)

    _contained_cwd = field_validator("cwd")(_relative_path)

    @field_validator("argv")
    @classmethod
    def _non_empty_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("argv entries must be non-empty and NUL-free")
        return value

    @model_validator(mode="after")
    def _environment_is_unambiguous_and_secret_safe(self) -> Self:
        names = [name for name, _value in self.environment]
        if len(names) != len(set(names)):
            raise ValueError("environment keys must be unique")
        if any(not name or "\x00" in name or "\x00" in value for name, value in self.environment):
            raise ValueError("environment entries must be non-empty and NUL-free")
        secret_markers = (
            "TOKEN",
            "SECRET",
            "PASSWORD",
            "CREDENTIAL",
            "API_KEY",
            "AUTH",
            "ACCESS_KEY",
            "PRIVATE_KEY",
        )
        secret_names = [
            name for name in names if any(marker in name.upper() for marker in secret_markers)
        ]
        if secret_names:
            raise ValueError(
                "credential-like environment values require opaque secret bindings: "
                f"{sorted(secret_names)}"
            )
        binding_names = [binding.name for binding in self.secret_bindings]
        if len(binding_names) != len(set(binding_names)):
            raise ValueError("secret binding names must be unique")
        return self

    @field_validator("timeout_seconds")
    @classmethod
    def _finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("timeout must be finite")
        return value


class DownloadRequest(DigestedRecordV2):
    schema_name: ClassVar[str] = "download_request"
    url: str = Field(pattern=r"^https://", max_length=4_096)
    purpose: str = Field(min_length=1, max_length=1_000)
    expected_media_type: str | None = Field(default=None, max_length=200)
    maximum_bytes: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    expected_sha256: Digest | None = None
    destination_kind: Identifier
    secret_bindings: tuple[SecretBinding, ...] = ()

    @field_validator("timeout_seconds")
    @classmethod
    def _finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("timeout must be finite")
        return value


class InstallRequest(DigestedRecordV2):
    schema_name: ClassVar[str] = "install_request"
    ecosystem: Literal["python_venv", "node_project"]
    operation: Literal[
        "existing_lock",
        "new_dependency",
        "manifest_lock_mutation",
        "lifecycle_scripts",
        "new_registry_domain",
        "host_global",
    ]
    manifest_path: RelativePath
    lock_path: RelativePath
    manifest_digest: Digest
    lock_digest: Digest
    manager_executable: RelativePath
    manager_version: str = Field(min_length=1, max_length=200)
    argv: tuple[str, ...] = Field(min_length=1)
    target: RelativePath
    network_required: bool = False
    lifecycle_scripts: bool = False
    expected_mutations: tuple[RelativePath, ...] = ()

    _paths = field_validator("manifest_path", "lock_path", "manager_executable", "target")(
        _relative_path
    )
    _mutation_paths = field_validator("expected_mutations")(
        lambda values: tuple(_relative_path(value) for value in values)
    )

    @field_validator("argv")
    @classmethod
    def _non_empty_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("argv entries must be non-empty and NUL-free")
        return value


class ArtifactDescriptorReference(SchemaModelV2):
    """Body-free, digest-complete reference to an authoritative descriptor."""

    schema_name: ClassVar[str] = "artifact_descriptor_reference"
    descriptor_id: Identifier
    descriptor_digest: Digest
    artifact_digest: Digest
    logical_kind: Identifier
    media_type: str = Field(min_length=1, max_length=200)
    size_bytes: int = Field(ge=0)
    producer_action_id: Identifier


class PredecessorOutputReference(SchemaModelV2):
    """Body-free binding to one exact PASS predecessor output."""

    schema_name: ClassVar[str] = "predecessor_output_reference"
    node_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    result_generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    worker_result_id: Identifier
    worker_result_digest: Digest
    artifact_descriptor_id: Identifier | None = None
    artifact_descriptor_digest: Digest | None = None
    artifact_digest: Digest | None = None
    artifact_descriptors: tuple[ArtifactDescriptorReference, ...] = ()
    evaluator_id: Identifier
    evaluator_digest: Digest
    evaluator_decision: Literal["PASS"] = "PASS"

    @model_validator(mode="after")
    def _artifact_reference_is_complete(self) -> Self:
        values = (
            self.artifact_descriptor_id,
            self.artifact_descriptor_digest,
            self.artifact_digest,
        )
        if any(value is None for value in values) and any(value is not None for value in values):
            raise ValueError("artifact descriptor and content bindings are all-or-none")
        if self.artifact_descriptors and self.artifact_descriptor_id is not None:
            first = self.artifact_descriptors[0]
            if (
                first.descriptor_id != self.artifact_descriptor_id
                or first.descriptor_digest != self.artifact_descriptor_digest
                or first.artifact_digest != self.artifact_digest
            ):
                raise ValueError("compatibility artifact fields must bind the first descriptor")
        return self


class WorkerRequest(DigestedRecordV2):
    schema_name: ClassVar[str] = "worker_request"
    goal: str = Field(min_length=1, max_length=20_000)
    accepted_plan_digest: Digest
    node_id: Identifier | None = None
    accepted_graph_revision_digest: Digest | None = None
    graph_run_id: Identifier | None = None
    generation: int = Field(default=0, ge=0)
    attempt: int = Field(default=0, ge=0)
    workspace_context: tuple[RelativePath, ...] = ()
    harness_digest: Digest
    effective_policy_digest: Digest
    remaining_budgets: CanonicalData
    prior_result_digests: tuple[Digest, ...] = ()
    prior_artifact_digests: tuple[Digest, ...] = ()
    predecessor_outputs: tuple[PredecessorOutputReference, ...] = ()

    _context_paths = field_validator("workspace_context")(
        lambda values: tuple(_relative_path(value) for value in values)
    )

    @model_validator(mode="after")
    def _graph_binding_is_complete(self) -> Self:
        if (self.node_id is None) != (self.accepted_graph_revision_digest is None):
            raise ValueError("node and accepted graph bindings must be supplied together")
        if (
            self.accepted_graph_revision_digest is not None
            and self.accepted_graph_revision_digest != self.accepted_plan_digest
        ):
            raise ValueError("accepted graph binding must match accepted_plan_digest")
        if self.predecessor_outputs:
            if (
                tuple(item.worker_result_digest for item in self.predecessor_outputs)
                != self.prior_result_digests
            ):
                raise ValueError("structured predecessor results must match compatibility digests")
            if (
                tuple(
                    item.artifact_digest
                    for item in self.predecessor_outputs
                    if item.artifact_digest is not None
                )
                != self.prior_artifact_digests
            ):
                raise ValueError(
                    "structured predecessor artifacts must match compatibility digests"
                )
            if any(
                item.accepted_graph_revision_digest != self.accepted_graph_revision_digest
                or item.generation != self.generation
                for item in self.predecessor_outputs
            ):
                raise ValueError("predecessor output is stale for this graph generation")
        return self


class WorkspaceRequest(DigestedRecordV2):
    schema_name: ClassVar[str] = "workspace_request"
    repository: str = Field(min_length=1, max_length=4_096)
    base_commit: Digest | str


class ApprovalRequest(DigestedRecordV2):
    schema_name: ClassVar[str] = "approval_request"
    request_digest: Digest
    policy_digest: Digest
    approval_classes: tuple[Identifier, ...] = Field(min_length=1)
    expires_at: UtcTimestamp

    @model_validator(mode="after")
    def _expiry_follows_creation(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("approval expiry must be after creation")
        return self


class ArtifactPutRequest(DigestedRecordV2):
    schema_name: ClassVar[str] = "artifact_put_request"
    media_type: str = Field(min_length=1, max_length=200)
    logical_kind: Identifier
    producer_action_id: Identifier
    source: CanonicalData
    redacted: bool = False


class EditIntentRequest(DigestedRecordV2):
    schema_name: ClassVar[str] = "edit_intent_request"
    paths: tuple[RelativePath, ...] = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=4_000)
    unified_diff: str = Field(min_length=1, max_length=1_000_000)

    _paths_are_relative = field_validator("paths")(
        lambda values: tuple(_relative_path(value) for value in values)
    )

    @model_validator(mode="after")
    def _patch_is_textual_and_paths_are_unique(self) -> Self:
        if "\x00" in self.unified_diff:
            raise ValueError("unified diff must not contain NUL bytes")
        if len(self.paths) != len(set(self.paths)):
            raise ValueError("edit paths must be unique")
        return self


class ReviewRequest(DigestedRecordV2):
    schema_name: ClassVar[str] = "review_request"
    candidate_digest: Digest
    verification_digests: tuple[Digest, ...] = ()
    purpose: str = Field(min_length=1, max_length=1_000)


ActionPayload = (
    ProcessRequest | DownloadRequest | InstallRequest | EditIntentRequest | ReviewRequest
)


class ActionKind(StableStrEnum):
    PROCESS = "process"
    DOWNLOAD = "download"
    INSTALL = "install"
    EDIT_INTENT = "edit_intent"
    REVIEW = "review"


class ActionProposal(DigestedRecordV2):
    schema_name: ClassVar[str] = "action_proposal"
    worker_id: Identifier
    kind: ActionKind
    payload: ActionPayload
    reason: str = Field(min_length=1, max_length=4_000)
    expected_artifact_kinds: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def _typed_payload_matches_kind(self) -> Self:
        expected = {
            ActionKind.PROCESS: ProcessRequest,
            ActionKind.DOWNLOAD: DownloadRequest,
            ActionKind.INSTALL: InstallRequest,
            ActionKind.EDIT_INTENT: EditIntentRequest,
            ActionKind.REVIEW: ReviewRequest,
        }[self.kind]
        if not isinstance(self.payload, expected):
            raise ValueError(f"{self.kind.value} proposal requires {expected.__name__}")
        return self


class DecisionOutcome(StableStrEnum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"


class PolicyDecision(DigestedRecordV2):
    schema_name: ClassVar[str] = "policy_decision"
    request_digest: Digest
    effective_policy_digest: Digest
    outcome: DecisionOutcome
    reason_code: Identifier
    limits: CanonicalData = None
    required_approval_classes: tuple[Identifier, ...] = ()


class ApprovalRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "approval_record"
    request_digest: Digest
    policy_digest: Digest
    scope: tuple[Digest, ...] = Field(min_length=1)
    decision: Literal["pending", "approved", "denied", "expired"]
    operator_label: str = Field(min_length=1, max_length=200)
    expires_at: UtcTimestamp
    decided_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def _decision_and_time_are_consistent(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("approval expiry must be after creation")
        if self.decision == "pending" and self.decided_at is not None:
            raise ValueError("pending approval cannot have a decision time")
        if self.decision != "pending" and self.decided_at is None:
            raise ValueError("terminal approval requires a decision time")
        if self.decided_at is not None and self.decided_at < self.created_at:
            raise ValueError("approval decision cannot predate creation")
        return self


class ArtifactDescriptor(DigestedRecordV2):
    schema_name: ClassVar[str] = "artifact_descriptor"
    artifact_digest: Digest
    media_type: str = Field(min_length=1, max_length=200)
    size_bytes: int = Field(ge=0)
    logical_kind: Identifier
    producer_action_id: Identifier
    source: CanonicalData
    redaction_state: Literal["none", "redacted", "secret"] = "none"
    store_locator: str = Field(min_length=1, max_length=4_096)


class ExecutionResult(DigestedRecordV2):
    schema_name: ClassVar[str] = "execution_result"
    request_digest: Digest
    status: Literal["succeeded", "failed", "cancelled", "indeterminate"]
    failure: StableFailure | None = None
    exit_code: int | None = None
    duration_seconds: float = Field(ge=0)
    resource_usage: CanonicalData = None
    stdout_artifact_digest: Digest | None = None
    stderr_artifact_digest: Digest | None = None

    @model_validator(mode="after")
    def _status_and_failure_are_consistent(self) -> Self:
        if self.status == "succeeded" and self.failure is not None:
            raise ValueError("successful execution cannot contain a failure")
        if self.status != "succeeded" and self.failure is None:
            raise ValueError("non-successful execution requires a stable failure")
        if not math.isfinite(self.duration_seconds):
            raise ValueError("execution duration must be finite")
        return self


class DownloadResult(ExecutionResult):
    schema_name: ClassVar[str] = "download_result"
    artifact: ArtifactDescriptor | None = None
    final_url: str | None = Field(default=None, max_length=4_096)


class InstallResult(ExecutionResult):
    schema_name: ClassVar[str] = "install_result"
    inventory_artifact_digest: Digest | None = None


class WorkerAvailability(DigestedRecordV2):
    schema_name: ClassVar[str] = "worker_availability"
    adapter: Identifier
    executable: str | None = Field(default=None, min_length=1, max_length=4_096)
    availability: Literal["available", "unavailable", "auth_unknown", "unknown"]
    auth: Literal["available", "unavailable", "unknown"]
    version: str | None = Field(default=None, max_length=200)
    failure: StableFailure | None = None


class WorkerResult(ExecutionResult):
    schema_name: ClassVar[str] = "worker_result"
    proposals: tuple[ActionProposal, ...] = ()
    assistant_note: str | None = Field(default=None, max_length=20_000)
    usage: CanonicalData = None


class WorkspaceSnapshot(DigestedRecordV2):
    schema_name: ClassVar[str] = "workspace_snapshot"
    repository_identity: Digest
    original_worktree: str = Field(min_length=1, max_length=4_096)
    head_commit: str = Field(min_length=1, max_length=200)
    base_tree: str = Field(min_length=1, max_length=200)
    dirty_state_digest: Digest
    isolated_worktree: str = Field(min_length=1, max_length=4_096)
    worktree_metadata: CanonicalData


class PromotionRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "promotion_record"
    base_identity: Digest
    reviewed_patch_digest: Digest
    verification_digest: Digest
    review_digest: Digest
    preflight_result_digest: Digest
    applied_tree_digest: Digest
    applied_diff_digest: Digest


class CriterionEvidence(SchemaModelV2):
    schema_name: ClassVar[str] = "criterion_evidence"
    criterion_id: Identifier
    disposition: Literal["satisfied", "blocked", "uncovered"]
    evidence_refs: tuple[Digest, ...] = ()


class AcceptanceLedger(DigestedRecordV2):
    schema_name: ClassVar[str] = "acceptance_ledger"
    criteria: tuple[CriterionEvidence, ...]
