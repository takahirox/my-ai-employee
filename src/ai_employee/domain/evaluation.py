"""Immutable v2 contracts for developer-managed artifact evaluation."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Protocol, Self

from pydantic import Field, model_validator

from .base import Digest, Identifier, StableStrEnum
from .v2 import ArtifactDescriptor, DigestedRecordV2, SchemaModelV2

PROCESS_EVALUATOR_ID = "process.harness"
RESERVED_EVALUATOR_IDS = frozenset(
    {"browser.playwright", "judge.visual", "threejs.instrumentation"}
)
AVAILABLE_FIRST_PARTY_EVALUATOR_IDS = frozenset({PROCESS_EVALUATOR_ID})


class EvaluatorBehavior(StableStrEnum):
    DETERMINISTIC = "deterministic"
    PROBABILISTIC = "probabilistic"


class CriterionOutcome(StableStrEnum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    INDETERMINATE = "indeterminate"


class FindingSeverity(StableStrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class EvaluationDecision(StableStrEnum):
    PASS = "PASS"
    REPAIR = "REPAIR"
    ESCALATE = "ESCALATE"
    FAIL = "FAIL"


class FreshnessMismatch(StableStrEnum):
    RUN = "run"
    CANDIDATE = "candidate"
    GENERATION = "generation"
    EVALUATOR_SPECIFICATION = "evaluator_specification"
    EFFECTIVE_POLICY = "effective_policy"
    REQUEST = "request"
    PROVIDER_DESCRIPTOR = "provider_descriptor"


class EvaluatorLimits(SchemaModelV2):
    schema_name: ClassVar[str] = "evaluator_limits"
    maximum_processes: int = Field(default=0, ge=0)
    maximum_artifact_bytes: int = Field(default=0, ge=0)
    maximum_observations: int = Field(default=0, ge=0)


class EvaluatorDescriptor(SchemaModelV2):
    schema_name: ClassVar[str] = "evaluator_descriptor"
    provider_id: Identifier
    provider_schema_version: Identifier
    behavior: EvaluatorBehavior
    required_capabilities: tuple[Identifier, ...] = ()
    supported_observation_kinds: tuple[Identifier, ...] = ()
    limits: EvaluatorLimits = EvaluatorLimits()

    @model_validator(mode="after")
    def _values_are_unique(self) -> Self:
        if len(self.required_capabilities) != len(set(self.required_capabilities)):
            raise ValueError("evaluator capabilities must be unique")
        if len(self.supported_observation_kinds) != len(set(self.supported_observation_kinds)):
            raise ValueError("evaluator observation kinds must be unique")
        return self


class EvaluatorSpecification(DigestedRecordV2):
    schema_name: ClassVar[str] = "evaluator_specification"
    provider_id: Identifier
    provider_schema_version: Identifier
    provider_descriptor_digest: Digest
    behavior: EvaluatorBehavior
    required_capabilities: tuple[Identifier, ...] = ()
    requested_observation_kinds: tuple[Identifier, ...] = ()
    command_ref: Identifier | None = None
    criterion_ids: tuple[Identifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _values_are_unique(self) -> Self:
        for label, values in (
            ("required capabilities", self.required_capabilities),
            ("requested observation kinds", self.requested_observation_kinds),
            ("criterion IDs", self.criterion_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"evaluator {label} must be unique")
        return self


class CandidateRevision(DigestedRecordV2):
    """Fleet-work candidate identity, distinct from AcceptedGraphRevision."""

    schema_name: ClassVar[str] = "candidate_revision"
    generation: int = Field(ge=0)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    candidate_patch_digest: Digest | None = None
    candidate_tree_digest: Digest | None = None

    @model_validator(mode="after")
    def _has_exact_candidate_content(self) -> Self:
        if (self.candidate_patch_digest is None) == (self.candidate_tree_digest is None):
            raise ValueError("candidate requires exactly one patch or tree digest")
        return self


class EvaluationBudget(SchemaModelV2):
    schema_name: ClassVar[str] = "evaluation_budget"
    remaining_processes: int = Field(default=0, ge=0)
    remaining_artifact_bytes: int = Field(default=0, ge=0)


class EvaluationRequest(DigestedRecordV2):
    schema_name: ClassVar[str] = "evaluation_request"
    candidate_digest: Digest
    generation: int = Field(ge=0)
    evaluator_specification_digest: Digest
    effective_policy_digest: Digest
    remaining_budget: EvaluationBudget = EvaluationBudget()


class ObservationManifest(DigestedRecordV2):
    schema_name: ClassVar[str] = "observation_manifest"
    request_digest: Digest
    candidate_digest: Digest
    generation: int = Field(ge=0)
    evaluator_specification_digest: Digest
    effective_policy_digest: Digest
    observation_kinds: tuple[Identifier, ...] = ()
    artifacts: tuple[ArtifactDescriptor, ...] = ()

    @model_validator(mode="after")
    def _artifacts_are_bound(self) -> Self:
        if any(item.run_id != self.run_id for item in self.artifacts):
            raise ValueError("observation artifact belongs to another run")
        descriptor_digests = tuple(item.content_digest for item in self.artifacts)
        if len(descriptor_digests) != len(set(descriptor_digests)):
            raise ValueError("observation artifact descriptors must be unique")
        kinds = tuple(item.logical_kind for item in self.artifacts)
        if self.observation_kinds != kinds or len(kinds) != len(set(kinds)):
            raise ValueError("observation kinds do not exactly describe the artifacts")
        return self


class EvaluationFinding(SchemaModelV2):
    schema_name: ClassVar[str] = "evaluation_finding"
    finding_id: Identifier
    code: Identifier
    severity: FindingSeverity
    message: str = Field(min_length=1, max_length=2_000)
    criterion_ids: tuple[Identifier, ...] = ()
    observation_artifact_digests: tuple[Digest, ...] = ()


class CriterionResult(SchemaModelV2):
    schema_name: ClassVar[str] = "criterion_result"
    criterion_id: Identifier
    outcome: CriterionOutcome
    explanation: str = Field(min_length=1, max_length=2_000)
    observation_artifact_digests: tuple[Digest, ...] = ()


class EvaluationResult(DigestedRecordV2):
    schema_name: ClassVar[str] = "evaluation_result"
    request_digest: Digest
    candidate_digest: Digest
    generation: int = Field(ge=0)
    evaluator_specification_digest: Digest
    effective_policy_digest: Digest
    provider_descriptor_digest: Digest
    behavior: EvaluatorBehavior
    expected_criterion_ids: tuple[Identifier, ...] = Field(min_length=1)
    observation_manifest: ObservationManifest
    execution_result_digest: Digest | None = None
    findings: tuple[EvaluationFinding, ...] = ()
    criterion_results: tuple[CriterionResult, ...] = ()

    @model_validator(mode="after")
    def _references_are_bound(self) -> Self:
        if self.observation_manifest.run_id != self.run_id:
            raise ValueError("observation manifest belongs to another run")
        if self.observation_manifest.request_digest != self.request_digest:
            raise ValueError("observation manifest belongs to another evaluation request")
        if self.observation_manifest.candidate_digest != self.candidate_digest:
            raise ValueError("observation manifest belongs to another candidate")
        if self.observation_manifest.generation != self.generation:
            raise ValueError("observation manifest belongs to another candidate generation")
        if (
            self.observation_manifest.evaluator_specification_digest
            != self.evaluator_specification_digest
        ):
            raise ValueError("observation manifest belongs to another evaluator specification")
        if self.observation_manifest.effective_policy_digest != self.effective_policy_digest:
            raise ValueError("observation manifest belongs to another effective policy")
        if len(self.expected_criterion_ids) != len(set(self.expected_criterion_ids)):
            raise ValueError("expected evaluation criterion IDs must be unique")
        criterion_ids = tuple(item.criterion_id for item in self.criterion_results)
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("evaluation criterion results must be unique")
        if set(criterion_ids) != set(self.expected_criterion_ids):
            raise ValueError("evaluation result does not exactly cover expected criteria")
        finding_criteria = {
            criterion_id for item in self.findings for criterion_id in item.criterion_ids
        }
        if not finding_criteria <= set(self.expected_criterion_ids):
            raise ValueError("evaluation finding references an unknown criterion")
        artifacts = {item.artifact_digest for item in self.observation_manifest.artifacts}
        references = {
            digest
            for item in self.criterion_results
            for digest in item.observation_artifact_digests
        }
        references.update(
            digest for item in self.findings for digest in item.observation_artifact_digests
        )
        if not references <= artifacts:
            raise ValueError("evaluation references an unknown observation artifact")
        return self


class EvaluationFreshness(SchemaModelV2):
    schema_name: ClassVar[str] = "evaluation_freshness"
    fresh: bool
    mismatches: tuple[FreshnessMismatch, ...] = ()

    @model_validator(mode="after")
    def _status_matches_reasons(self) -> Self:
        if self.fresh == bool(self.mismatches):
            raise ValueError("freshness status and mismatch reasons disagree")
        return self


def evaluate_freshness(
    candidate: CandidateRevision,
    specification: EvaluatorSpecification,
    effective_policy_digest: Digest,
    request: EvaluationRequest,
    result: EvaluationResult,
) -> EvaluationFreshness:
    """Compare every immutable freshness fence without mutable runtime state."""

    mismatches: list[FreshnessMismatch] = []
    candidate_digest = candidate.content_digest or ""
    specification_digest = specification.content_digest or ""
    if len({candidate.run_id, specification.run_id, request.run_id, result.run_id}) != 1:
        mismatches.append(FreshnessMismatch.RUN)
    if request.candidate_digest != candidate_digest or result.candidate_digest != candidate_digest:
        mismatches.append(FreshnessMismatch.CANDIDATE)
    if request.generation != candidate.generation or result.generation != candidate.generation:
        mismatches.append(FreshnessMismatch.GENERATION)
    if (
        request.evaluator_specification_digest != specification_digest
        or result.evaluator_specification_digest != specification_digest
    ):
        mismatches.append(FreshnessMismatch.EVALUATOR_SPECIFICATION)
    if (
        request.effective_policy_digest != effective_policy_digest
        or result.effective_policy_digest != effective_policy_digest
    ):
        mismatches.append(FreshnessMismatch.EFFECTIVE_POLICY)
    if result.request_digest != request.content_digest:
        mismatches.append(FreshnessMismatch.REQUEST)
    if result.provider_descriptor_digest != specification.provider_descriptor_digest:
        mismatches.append(FreshnessMismatch.PROVIDER_DESCRIPTOR)
    return EvaluationFreshness(
        fresh=not mismatches,
        mismatches=tuple(dict.fromkeys(mismatches)),
    )


def decide_evaluation(
    criterion_results: tuple[CriterionResult, ...],
    findings: tuple[EvaluationFinding, ...],
    freshness: EvaluationFreshness,
    remaining_budget: EvaluationBudget,
    behavior: EvaluatorBehavior,
    *,
    expected_criterion_ids: tuple[Identifier, ...],
) -> EvaluationDecision:
    """Return the deterministic authority decision for validated evidence."""

    if not freshness.fresh:
        return EvaluationDecision.FAIL
    criterion_ids = tuple(item.criterion_id for item in criterion_results)
    if len(criterion_ids) != len(set(criterion_ids)) or set(criterion_ids) != set(
        expected_criterion_ids
    ):
        return EvaluationDecision.FAIL
    if any(item.severity is FindingSeverity.CRITICAL for item in findings):
        return EvaluationDecision.FAIL
    if behavior is EvaluatorBehavior.PROBABILISTIC:
        return EvaluationDecision.ESCALATE
    if any(item.outcome is CriterionOutcome.INDETERMINATE for item in criterion_results):
        return EvaluationDecision.ESCALATE
    needs_repair = any(
        item.outcome is CriterionOutcome.UNSATISFIED for item in criterion_results
    ) or any(item.severity is FindingSeverity.HIGH for item in findings)
    if needs_repair:
        if (
            remaining_budget.remaining_processes > 0
            and remaining_budget.remaining_artifact_bytes > 0
        ):
            return EvaluationDecision.REPAIR
        return EvaluationDecision.FAIL
    return EvaluationDecision.PASS


class EvaluationEvidenceLedger(DigestedRecordV2):
    """Replayable evaluation evidence, separate from AcceptanceLedger v2."""

    schema_name: ClassVar[str] = "evaluation_evidence_ledger"
    candidate_digest: Digest
    generation: int = Field(ge=0)
    evaluator_specification_digest: Digest
    effective_policy_digest: Digest
    evaluation_result_digests: tuple[Digest, ...] = Field(min_length=1)
    observation_manifest_digests: tuple[Digest, ...] = ()
    expected_criterion_ids: tuple[Identifier, ...] = Field(min_length=1)
    criterion_results: tuple[CriterionResult, ...]
    findings: tuple[EvaluationFinding, ...] = ()
    freshness: EvaluationFreshness
    remaining_budget: EvaluationBudget
    behavior: EvaluatorBehavior
    decision: EvaluationDecision

    @model_validator(mode="after")
    def _decision_is_replayable(self) -> Self:
        if len(self.expected_criterion_ids) != len(set(self.expected_criterion_ids)):
            raise ValueError("expected evaluation criterion IDs must be unique")
        expected = decide_evaluation(
            self.criterion_results,
            self.findings,
            self.freshness,
            self.remaining_budget,
            self.behavior,
            expected_criterion_ids=self.expected_criterion_ids,
        )
        if self.decision is not expected:
            raise ValueError("persisted evaluation decision does not match its evidence")
        return self


def replay_evaluation_decision(ledger: EvaluationEvidenceLedger) -> EvaluationDecision:
    return decide_evaluation(
        ledger.criterion_results,
        ledger.findings,
        ledger.freshness,
        ledger.remaining_budget,
        ledger.behavior,
        expected_criterion_ids=ledger.expected_criterion_ids,
    )


class EvaluatorServices(Protocol):
    """Narrow mediated surface available to first-party evaluator code."""

    def new_id(self, prefix: str) -> Identifier: ...

    def created_at(self) -> datetime: ...

    def execute_declared_process(
        self, command_ref: Identifier, request: EvaluationRequest
    ) -> object: ...

    def artifact_descriptor(
        self,
        artifact_digest: Digest,
        logical_kind: Identifier,
        producer_execution_id: Identifier,
    ) -> ArtifactDescriptor: ...


class EvaluatorProvider(Protocol):
    @property
    def descriptor(self) -> EvaluatorDescriptor: ...

    def evaluate(
        self,
        request: EvaluationRequest,
        specification: EvaluatorSpecification,
        services: EvaluatorServices,
    ) -> EvaluationResult: ...
