"""Deterministic, fail-closed authority for low-risk graph promotion approval."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from typing import ClassVar, Literal, TypeVar

from pydantic import Field, model_validator

from .config import OperatorConfig, PromotionAutoApprovalConfig
from .domain import ProjectHarnessV2
from .domain.base import Digest, Identifier
from .domain.browser import BrowserObservation
from .domain.evaluation import (
    EvaluationDecision,
    EvaluationEvidenceLedger,
    EvaluationRequest,
    EvaluationResult,
    EvaluatorSpecification,
    ObservationManifest,
)
from .domain.models import AcceptedGraphRevision
from .domain.v2 import (
    AcceptanceLedger,
    ApprovalRecord,
    ArtifactDescriptor,
    DigestedRecordV2,
    ExecutionResult,
    ProcessRequest,
)
from .evaluators import DEFAULT_EVALUATOR_REGISTRY
from .graph_composition import GraphPatchCompositionRecord
from .graph_evaluation import (
    ParentCandidateEvaluationRecord,
    ParentCandidateEvaluationReplay,
    ParentCandidateEvaluationRequest,
)
from .parent_review import (
    ParentSemanticReviewDecision,
    ParentSemanticReviewRequest,
    ParentSemanticReviewResult,
    ParentSemanticSeverity,
    decide_parent_semantic_review,
    validate_parent_semantic_review_result,
)
from .serialization import canonical_digest, versioned_digest
from .storage import SQLiteStore
from .task_orchestration import GoalEvaluatorRecord, GraphRunRecord

PROMOTION_AUTO_APPROVAL_RULE_ID = "low-risk-exact-evidence-v1"
RecordT = TypeVar("RecordT", bound=DigestedRecordV2)

_CONTROL_OR_DEPENDENCY_PATTERNS = (
    ".fleet/**",
    ".github/**",
    "pyproject.toml",
    "requirements*.txt",
    "uv.lock",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Gemfile",
    "Gemfile.lock",
)


class PromotionPolicyDecision(DigestedRecordV2):
    """Inspectable Trust Kernel decision; it never performs promotion itself."""

    schema_name: ClassVar[str] = "promotion_policy_decision"
    mode: Literal["manual", "policy"]
    decision: Literal["manual_required", "policy_auto_approved"]
    rule_id: Identifier
    reason_code: Identifier
    rule_config_digest: Digest
    candidate_digest: Digest
    accepted_graph_revision_digest: Digest
    graph_generation: int = Field(ge=0)
    composition_digest: Digest
    harness_digest: Digest
    effective_policy_digest: Digest
    operator_config_digest: Digest
    repository: str = Field(min_length=1, max_length=4_096)
    parent_evaluation_digest: Digest
    goal_evaluator_digest: Digest
    verification_evidence_digests: tuple[Digest, ...] = ()
    evaluation_ledger_digests: tuple[Digest, ...] = ()
    semantic_evidence_digests: tuple[Digest, ...] = ()
    node_fact_digests: tuple[Digest, ...] = ()
    changed_paths: tuple[str, ...] = ()
    maximum_node_risk: int = Field(ge=0, le=10)
    changed_files: int = Field(ge=0)
    patch_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def _authority_is_canonical(self) -> PromotionPolicyDecision:
        if self.changed_paths != tuple(sorted(self.changed_paths)):
            raise ValueError("promotion changed paths must be sorted")
        if self.changed_files != len(self.changed_paths):
            raise ValueError("promotion changed-file count is stale")
        if self.decision == "policy_auto_approved":
            for values in (
                self.verification_evidence_digests,
                self.evaluation_ledger_digests,
                self.semantic_evidence_digests,
                self.node_fact_digests,
                self.changed_paths,
            ):
                if len(values) != len(set(values)):
                    raise ValueError("auto-approval evidence must be unique")
            if self.mode != "policy" or self.reason_code != "eligible_low_risk_exact_evidence":
                raise ValueError("auto-approval must come from the bounded policy rule")
            if not self.verification_evidence_digests or not self.evaluation_ledger_digests:
                raise ValueError("auto-approval requires deterministic evidence")
        return self


class PromotionApprovalTrustKernel:
    """Resolve one conservative auto-approval rule from immutable evidence."""

    def __init__(
        self,
        harness: ProjectHarnessV2,
        operator_policy: PromotionAutoApprovalConfig,
        *,
        harness_digest: Digest,
        operator_config_digest: Digest,
    ) -> None:
        self.harness = harness
        self.operator_policy = operator_policy
        self.harness_digest = harness_digest
        self.operator_config_digest = operator_config_digest

    def resolve(
        self,
        run: GraphRunRecord,
        accepted_revision: AcceptedGraphRevision,
        composition: GraphPatchCompositionRecord,
        evaluation: ParentCandidateEvaluationRecord,
        replay: ParentCandidateEvaluationReplay | None,
        *,
        evidence_storage_valid: bool = False,
    ) -> PromotionPolicyDecision:
        paths = tuple(sorted({path for item in composition.ordered_inputs for path in item.paths}))
        accepted_nodes = tuple(sorted(accepted_revision.graph.nodes, key=lambda item: item.id))
        node_fact_digests = tuple(
            canonical_digest(
                {
                    "id": node.id,
                    "risk": node.risk,
                    "capabilities": node.required_capabilities,
                }
            )
            for node in accepted_nodes
        )
        maximum_risk = max((node.risk for node in accepted_nodes), default=10)
        semantic = tuple(
            digest
            for record in (
                *(() if replay is None else replay.semantic_requests),
                *(() if replay is None else replay.semantic_results),
                *(() if replay is None else replay.semantic_decisions),
                *(() if replay is None else replay.semantic_repair_requests),
            )
            if (digest := record.content_digest) is not None
        )
        reason = self._reason(
            run,
            accepted_revision,
            composition,
            evaluation,
            replay,
            paths,
            maximum_risk,
            semantic,
            evidence_storage_valid,
        )
        mode = self.operator_policy.mode
        return PromotionPolicyDecision(
            id=f"promotion-policy-{canonical_digest((run.id, evaluation.content_digest))[:24]}",
            run_id=run.id,
            created_at=evaluation.created_at,
            mode=mode,
            decision=(
                "policy_auto_approved"
                if reason == "eligible_low_risk_exact_evidence"
                else "manual_required"
            ),
            rule_id=PROMOTION_AUTO_APPROVAL_RULE_ID,
            reason_code=reason,
            rule_config_digest=canonical_digest(
                {
                    "rule_id": PROMOTION_AUTO_APPROVAL_RULE_ID,
                    "operator": self.operator_policy,
                    "project_promotion": self.harness.approvals.promotion,
                }
            ),
            candidate_digest=evaluation.candidate_artifact_digest,
            accepted_graph_revision_digest=evaluation.accepted_graph_revision_digest,
            graph_generation=accepted_revision.revision_number,
            composition_digest=_required(composition.content_digest),
            harness_digest=self.harness_digest,
            effective_policy_digest=evaluation.effective_policy_digest,
            operator_config_digest=self.operator_config_digest,
            repository=run.repository or "unavailable",
            parent_evaluation_digest=_required(evaluation.content_digest),
            goal_evaluator_digest=evaluation.goal_evaluator_digest,
            verification_evidence_digests=evaluation.verification_result_digests,
            evaluation_ledger_digests=evaluation.evaluation_ledger_digests,
            semantic_evidence_digests=semantic,
            node_fact_digests=node_fact_digests,
            changed_paths=paths,
            maximum_node_risk=maximum_risk,
            changed_files=len(paths),
            patch_bytes=(
                0 if composition.candidate_patch is None else composition.candidate_patch.size_bytes
            ),
        )

    def _reason(
        self,
        run: GraphRunRecord,
        accepted_revision: AcceptedGraphRevision,
        composition: GraphPatchCompositionRecord,
        evaluation: ParentCandidateEvaluationRecord,
        replay: ParentCandidateEvaluationReplay | None,
        paths: tuple[str, ...],
        maximum_risk: int,
        semantic: tuple[Digest, ...],
        evidence_storage_valid: bool,
    ) -> Identifier:
        if self.operator_policy.mode != "policy":
            return "manual_mode_default"
        if self.harness.approvals.promotion != "policy":
            return "project_policy_opt_in_missing"
        if replay is None:
            return "evidence_replay_unavailable"
        if not evidence_storage_valid:
            return "evidence_storage_invalid"
        if self.harness.provisional:
            return "provisional_harness"
        repository = run.repository
        if repository is None or str(Path(repository).resolve()) not in {
            str(Path(item).resolve()) for item in self.operator_policy.allowed_repositories
        }:
            return "repository_not_allowed"
        if (
            run.harness_digest != self.harness_digest
            or run.operator_config_digest != self.operator_config_digest
            or evaluation != replay.record
            or accepted_revision.content_digest != run.accepted_graph_revision_digest
            or evaluation.status != "ready_to_promote"
            or evaluation.decision is not EvaluationDecision.PASS
            or evaluation.content_digest != run.parent_evaluation_digest
            or evaluation.accepted_graph_revision_digest != run.accepted_graph_revision_digest
            or evaluation.composition_record_digest != composition.content_digest
            or evaluation.candidate_artifact_digest != run.parent_candidate_digest
            or evaluation.effective_policy_digest != run.effective_policy_digest
        ):
            return "stale_or_mismatched_authority"
        if maximum_risk > self.operator_policy.max_risk:
            return "risk_limit_exceeded"
        capabilities = {
            capability
            for node in accepted_revision.graph.nodes
            for capability in node.required_capabilities
        }
        if (
            self.harness.network.mode.value != "disabled"
            or self.harness.install.ecosystems
            or capabilities & {"download", "install"}
        ):
            return "network_or_install_side_effect"
        if not paths or composition.candidate_patch is None:
            return "candidate_change_facts_missing"
        if len(paths) > self.operator_policy.max_changed_files:
            return "changed_file_limit_exceeded"
        if composition.candidate_patch.size_bytes > self.operator_policy.max_patch_bytes:
            return "patch_size_limit_exceeded"
        denied_patterns = (*self.harness.paths.protected, *_CONTROL_OR_DEPENDENCY_PATTERNS)
        if any(_matches(path, pattern) for path in paths for pattern in denied_patterns):
            return "protected_or_control_path"
        required_count = len(self.harness.verification.required_evaluators)
        if (
            required_count == 0
            or len(evaluation.verification_result_digests)
            != len(set(evaluation.verification_result_digests))
            or len(evaluation.evaluation_ledger_digests)
            != len(set(evaluation.evaluation_ledger_digests))
            or len(evaluation.verification_result_digests) != required_count
            or len(evaluation.evaluation_ledger_digests) != required_count
            or len(replay.evaluation_ledgers) != required_count
            or any(
                ledger.decision is not EvaluationDecision.PASS or not ledger.freshness.fresh
                for ledger in replay.evaluation_ledgers
            )
            or any(item.disposition != "satisfied" for item in replay.acceptance_ledger.criteria)
        ):
            return "deterministic_evidence_incomplete"
        if self.harness.verification.review.parent_semantic_review:
            if (
                len(semantic) != len(set(semantic))
                or len(replay.semantic_requests) != 1
                or len(replay.semantic_results) != 1
                or len(replay.semantic_decisions) != 1
                or replay.semantic_decisions[0].action is not EvaluationDecision.PASS
                or replay.semantic_repair_requests
            ):
                return "semantic_review_not_clean"
            if not semantic:
                return "semantic_evidence_missing"
        elif semantic:
            return "unexpected_semantic_evidence"
        return "eligible_low_risk_exact_evidence"


def validate_exact_parent_evidence_store(
    store: SQLiteStore,
    run: GraphRunRecord,
    accepted_revision: AcceptedGraphRevision,
    evaluation: ParentCandidateEvaluationRecord,
    harness: ProjectHarnessV2,
) -> ParentCandidateEvaluationReplay:
    """Load every promotion evidence digest exactly once and verify its run/bindings."""

    run_id = run.id
    evaluation_digest = _required(evaluation.content_digest)
    stored_evaluation = _exact_content_record(
        store,
        "parent_candidate_evaluation_v2",
        ParentCandidateEvaluationRecord,
        run_id,
        evaluation_digest,
    )
    request = _exact_content_record(
        store,
        "parent_candidate_evaluation_request_v2",
        ParentCandidateEvaluationRequest,
        run_id,
        evaluation.request_digest,
    )
    accepted_digest = _required(accepted_revision.content_digest)
    candidate_digest = _required(request.candidate.content_digest)
    if (
        stored_evaluation != evaluation
        or evaluation.run_id != run_id
        or request.run_id != run_id
        or request.accepted_revision != accepted_revision
        or request.accepted_revision.content_digest != evaluation.accepted_graph_revision_digest
        or evaluation.accepted_graph_revision_digest != accepted_digest
        or request.composition_record_digest != evaluation.composition_record_digest
        or request.composition_workspace.content_digest != evaluation.composition_workspace_digest
        or candidate_digest != evaluation.candidate_digest
        or request.candidate_artifact.content_digest != evaluation.candidate_descriptor_digest
        or request.candidate_artifact.artifact_digest != evaluation.candidate_artifact_digest
        or request.effective_policy_digest != evaluation.effective_policy_digest
        or request.harness_digest != run.harness_digest
        or evaluation.effective_policy_digest != run.effective_policy_digest
        or evaluation.status != "ready_to_promote"
        or evaluation.decision is not EvaluationDecision.PASS
    ):
        raise ValueError("parent evaluation store bindings are stale")
    _validate_harness_evaluator_bindings(request, harness)
    process_requests = tuple(
        item.process_request
        for item in request.verification_bindings
        if item.process_request is not None
    )
    process_request_digests = tuple(_required(item.content_digest) for item in process_requests)
    if process_request_digests != evaluation.verification_request_digests:
        raise ValueError("parent verification request bindings are stale")
    for process_request, digest in zip(process_requests, process_request_digests, strict=True):
        if (
            _exact_content_record(
                store,
                "verification_request_v2",
                ProcessRequest,
                run_id,
                digest,
            )
            != process_request
        ):
            raise ValueError("parent verification request store binding is stale")

    goal_evaluator = _exact_content_record(
        store,
        "goal_evaluator_v2",
        GoalEvaluatorRecord,
        run_id,
        evaluation.goal_evaluator_digest,
    )
    if (
        goal_evaluator.accepted_graph_revision_digest != accepted_digest
        or goal_evaluator.goal_id != request.goal.id
        or goal_evaluator.decision is not EvaluationDecision.PASS
        or not goal_evaluator.evidence_digests
        or len(goal_evaluator.evidence_digests) != len(set(goal_evaluator.evidence_digests))
    ):
        raise ValueError("parent Goal evaluator store bindings are stale")
    acceptance = _exact_content_record(
        store,
        "acceptance_ledger_v2",
        AcceptanceLedger,
        run_id,
        goal_evaluator.evidence_digests[0],
    )
    if tuple(item.criterion_id for item in acceptance.criteria) != tuple(
        item.id for item in request.goal.completion_criteria
    ) or any(
        item.disposition != "satisfied" or not item.evidence_refs for item in acceptance.criteria
    ):
        raise ValueError("parent AcceptanceLedger is incomplete")

    if (
        not evaluation.evaluation_ledger_digests
        or len(evaluation.evaluation_ledger_digests)
        != len(set(evaluation.evaluation_ledger_digests))
        or len(evaluation.verification_result_digests)
        != len(set(evaluation.verification_result_digests))
        or len(evaluation.evaluation_ledger_digests) != len(request.verification_bindings)
    ):
        raise ValueError("parent deterministic evidence references are ambiguous")

    ledgers: list[EvaluationEvidenceLedger] = []
    runtime_digests: list[Digest] = []
    authoritative_evidence: set[Digest] = set()
    expected_spec_digests = tuple(
        _required(item.specification.content_digest) for item in request.verification_bindings
    )
    for ledger_digest, binding in zip(
        evaluation.evaluation_ledger_digests, request.verification_bindings, strict=True
    ):
        ledger = _exact_content_record(
            store,
            "evaluation_evidence_ledger_v2",
            EvaluationEvidenceLedger,
            run_id,
            ledger_digest,
        )
        specification_digest = _required(binding.specification.content_digest)
        specification = _exact_content_record(
            store,
            "evaluator_specification_v2",
            EvaluatorSpecification,
            run_id,
            specification_digest,
        )
        if (
            specification != binding.specification
            or ledger.candidate_digest != candidate_digest
            or ledger.generation != request.candidate.generation
            or ledger.effective_policy_digest != request.effective_policy_digest
            or ledger.evaluator_specification_digest != specification_digest
            or ledger.decision is not EvaluationDecision.PASS
            or not ledger.freshness.fresh
            or len(ledger.evaluation_result_digests) != len(set(ledger.evaluation_result_digests))
            or len(ledger.observation_manifest_digests)
            != len(set(ledger.observation_manifest_digests))
            or len(ledger.evaluation_result_digests) != 1
            or len(ledger.observation_manifest_digests) != 1
        ):
            raise ValueError("evaluation ledger store bindings are stale")
        result = _exact_content_record(
            store,
            "evaluation_result_v2",
            EvaluationResult,
            run_id,
            ledger.evaluation_result_digests[0],
        )
        evaluation_request = _exact_content_record(
            store,
            "evaluation_request_v2",
            EvaluationRequest,
            run_id,
            result.request_digest,
        )
        manifest_digest = _required(result.observation_manifest.content_digest)
        manifest = _exact_content_record(
            store,
            "observation_manifest_v2",
            ObservationManifest,
            run_id,
            manifest_digest,
        )
        if (
            result.observation_manifest != manifest
            or ledger.observation_manifest_digests != (manifest_digest,)
            or evaluation_request.candidate_digest != candidate_digest
            or evaluation_request.generation != request.candidate.generation
            or evaluation_request.effective_policy_digest != request.effective_policy_digest
            or evaluation_request.evaluator_specification_digest != specification_digest
            or result.candidate_digest != candidate_digest
            or result.generation != request.candidate.generation
            or result.effective_policy_digest != request.effective_policy_digest
            or result.evaluator_specification_digest != specification_digest
            or result.provider_descriptor_digest != specification.provider_descriptor_digest
            or result.behavior != specification.behavior
            or result.expected_criterion_ids != specification.criterion_ids
            or ledger.expected_criterion_ids != specification.criterion_ids
            or ledger.criterion_results != result.criterion_results
            or ledger.findings != result.findings
            or ledger.remaining_budget != evaluation_request.remaining_budget
            or ledger.behavior != result.behavior
            or result.execution_result_digest is None
        ):
            raise ValueError("evaluation result store bindings are stale")
        _exact_runtime_record(store, run_id, result.execution_result_digest)
        for artifact in manifest.artifacts:
            persisted = _exact_artifact_record(store, run_id, artifact.artifact_digest)
            if persisted != artifact:
                raise ValueError("observation artifact store binding is stale")
            authoritative_evidence.add(artifact.artifact_digest)
        authoritative_evidence.update(
            (ledger_digest, _required(result.content_digest), manifest_digest)
        )
        runtime_digests.append(result.execution_result_digest)
        ledgers.append(ledger)

    if (
        tuple(item.evaluator_specification_digest for item in ledgers) != expected_spec_digests
        or tuple(runtime_digests) != evaluation.verification_result_digests
    ):
        raise ValueError("parent evaluator ordering or runtime evidence is stale")
    prefix = (_required(acceptance.content_digest), *evaluation.evaluation_ledger_digests)
    if goal_evaluator.evidence_digests[: len(prefix)] != prefix:
        raise ValueError("parent Goal evidence prefix is stale")
    semantic_digests = goal_evaluator.evidence_digests[len(prefix) :]
    semantic_requests: tuple[ParentSemanticReviewRequest, ...] = ()
    semantic_results: tuple[ParentSemanticReviewResult, ...] = ()
    semantic_decisions: tuple[ParentSemanticReviewDecision, ...] = ()
    accepted_semantic_findings: tuple[Digest, ...] = ()
    if harness.verification.review.parent_semantic_review:
        if len(semantic_digests) != 3 or len(semantic_digests) != len(set(semantic_digests)):
            raise ValueError("parent semantic evidence is missing or ambiguous")
        semantic_request = _exact_content_record(
            store,
            "parent_semantic_review_request_v2",
            ParentSemanticReviewRequest,
            run_id,
            semantic_digests[0],
        )
        semantic_result = _exact_content_record(
            store,
            "parent_semantic_review_result_v2",
            ParentSemanticReviewResult,
            run_id,
            semantic_digests[1],
        )
        semantic_decision = _exact_content_record(
            store,
            "parent_semantic_review_decision_v2",
            ParentSemanticReviewDecision,
            run_id,
            semantic_digests[2],
        )
        validate_parent_semantic_review_result(semantic_request, semantic_result)
        expected_decision = decide_parent_semantic_review(
            semantic_request,
            semantic_result,
            block_severities=tuple(
                ParentSemanticSeverity(item)
                for item in harness.verification.review.block_severities
            ),
            decision_id=semantic_decision.id,
            run_id=semantic_decision.run_id,
            created_at=semantic_decision.created_at,
        )
        if (
            semantic_request.accepted_graph_revision_digest != accepted_digest
            or semantic_request.candidate_artifact_digest != evaluation.candidate_artifact_digest
            or semantic_request.effective_policy_digest != evaluation.effective_policy_digest
            or semantic_request.deterministic_ledger_digests != evaluation.evaluation_ledger_digests
            or semantic_request.deterministic_ledgers != tuple(ledgers)
            or semantic_decision != expected_decision
            or semantic_decision.action is not EvaluationDecision.PASS
        ):
            raise ValueError("parent semantic evidence store bindings are stale")
        semantic_requests = (semantic_request,)
        semantic_results = (semantic_result,)
        semantic_decisions = (semantic_decision,)
        accepted_semantic_findings = semantic_decision.accepted_finding_digests
        authoritative_evidence.update(semantic_digests)
    elif semantic_digests:
        raise ValueError("unexpected parent semantic evidence")

    authoritative_evidence.update(accepted_semantic_findings)
    if any(not set(item.evidence_refs) <= authoritative_evidence for item in acceptance.criteria):
        raise ValueError("AcceptanceLedger cites non-authoritative evidence")
    return ParentCandidateEvaluationReplay(
        record=evaluation,
        acceptance_ledger=acceptance,
        evaluation_ledgers=tuple(ledgers),
        semantic_requests=semantic_requests,
        semantic_results=semantic_results,
        semantic_decisions=semantic_decisions,
    )


def _exact_content_record(
    store: SQLiteStore,
    kind: str,
    model_type: type[RecordT],
    run_id: Identifier,
    digest: Digest,
) -> RecordT:
    matches = tuple(
        item
        for item in store.list_records(kind, model_type, run_id=run_id)
        if item.content_digest == digest
    )
    if len(matches) != 1 or matches[0].run_id != run_id:
        raise ValueError(f"{kind} digest is missing, foreign, or ambiguous")
    return matches[0]


def _validate_harness_evaluator_bindings(
    request: ParentCandidateEvaluationRequest,
    harness: ProjectHarnessV2,
) -> None:
    binding_ids = tuple(item.harness_evaluator_id for item in request.verification_bindings)
    if binding_ids != harness.verification.required_evaluators:
        raise ValueError("parent evaluator IDs do not match the required Harness order")
    declarations = {item.id: item for item in harness.evaluators}
    if len(declarations) != len(harness.evaluators):
        raise ValueError("Harness evaluator declarations are ambiguous")
    for binding in request.verification_bindings:
        declaration = declarations.get(binding.harness_evaluator_id)
        if declaration is None:
            raise ValueError("required Harness evaluator declaration is missing")
        specification = binding.specification
        descriptor = DEFAULT_EVALUATOR_REGISTRY.resolve(declaration.provider_id).descriptor
        if (
            specification.provider_id != declaration.provider_id
            or specification.provider_id != descriptor.provider_id
            or specification.provider_schema_version != descriptor.provider_schema_version
            or specification.provider_descriptor_digest != versioned_digest(descriptor)
            or specification.behavior != descriptor.behavior
            or specification.required_capabilities != descriptor.required_capabilities
            or specification.criterion_ids != declaration.criterion_ids
        ):
            raise ValueError("parent evaluator specification is not the required Harness provider")
        if declaration.provider_id == "browser.playwright":
            scenario = declaration.browser_scenario
            if (
                scenario is None
                or binding.harness_command_ref is not None
                or binding.process_request is not None
                or specification.command_ref is not None
                or specification.browser_scenario != scenario
                or specification.requested_observation_kinds
                != tuple(item.logical_kind for item in scenario.captures)
            ):
                raise ValueError("parent browser evaluator does not match the Harness scenario")
            continue
        command_ref = declaration.command_ref
        process_request = binding.process_request
        if command_ref is None or process_request is None:
            raise ValueError("parent process evaluator lacks its Harness command")
        command = harness.commands.get(command_ref)
        if (
            command is None
            or binding.harness_command_ref != command_ref
            or specification.command_ref != process_request.id
            or specification.browser_scenario is not None
            or specification.requested_observation_kinds
            or process_request.argv != command.argv
            or process_request.cwd != command.cwd
            or process_request.inherit_environment != command.inherit_environment
            or process_request.timeout_seconds != min(300.0, harness.budgets.wall_seconds)
            or process_request.budget_class != "parent-verification"
            or process_request.purpose != f"required parent Harness verification: {command_ref}"
        ):
            raise ValueError("parent process evaluator does not match the Harness command")


def _exact_runtime_record(store: SQLiteStore, run_id: Identifier, digest: Digest) -> None:
    process = tuple(
        item
        for item in store.list_records("verification_result_v2", ExecutionResult, run_id=run_id)
        if item.content_digest == digest
    )
    browser = tuple(
        item
        for item in store.list_records("browser_observation_v2", BrowserObservation, run_id=run_id)
        if item.content_digest == digest
    )
    matches: tuple[ExecutionResult | BrowserObservation, ...] = (*process, *browser)
    if len(matches) != 1 or matches[0].run_id != run_id or matches[0].status != "succeeded":
        raise ValueError("runtime evidence digest is missing, foreign, or ambiguous")


def _exact_artifact_record(
    store: SQLiteStore, run_id: Identifier, artifact_digest: Digest
) -> ArtifactDescriptor:
    matches = tuple(
        item
        for item in store.list_records("artifact_descriptor_v2", ArtifactDescriptor, run_id=run_id)
        if item.artifact_digest == artifact_digest
    )
    if len(matches) != 1 or matches[0].run_id != run_id:
        raise ValueError("artifact digest is missing, foreign, or ambiguous")
    return matches[0]


def _matches(path: str, pattern: str) -> bool:
    return fnmatchcase(path, pattern) or Path(path).match(pattern)


def validate_policy_auto_authority(
    approval: ApprovalRecord,
    authority: PromotionPolicyDecision,
    run: GraphRunRecord,
    accepted_revision: AcceptedGraphRevision,
    composition: GraphPatchCompositionRecord,
    evaluation: ParentCandidateEvaluationRecord,
    harness: ProjectHarnessV2,
    operator_config: OperatorConfig,
    semantic_evidence_digests: tuple[Digest, ...],
    *,
    harness_digest: Digest,
    operator_config_digest: Digest,
) -> None:
    """Recompute non-probabilistic promotion facts immediately before mutation."""

    policy = operator_config.promotion_auto_approval
    paths = tuple(sorted({path for item in composition.ordered_inputs for path in item.paths}))
    nodes = tuple(sorted(accepted_revision.graph.nodes, key=lambda item: item.id))
    node_fact_digests = tuple(
        canonical_digest(
            {
                "id": node.id,
                "risk": node.risk,
                "capabilities": node.required_capabilities,
            }
        )
        for node in nodes
    )
    maximum_risk = max((node.risk for node in nodes), default=10)
    capabilities = {
        capability
        for node in accepted_revision.graph.nodes
        for capability in node.required_capabilities
    }
    rule_config_digest = canonical_digest(
        {
            "rule_id": PROMOTION_AUTO_APPROVAL_RULE_ID,
            "operator": policy,
            "project_promotion": harness.approvals.promotion,
        }
    )
    denied_patterns = (*harness.paths.protected, *_CONTROL_OR_DEPENDENCY_PATTERNS)
    expected = {
        "candidate_digest": evaluation.candidate_artifact_digest,
        "accepted_graph_revision_digest": _required(accepted_revision.content_digest),
        "graph_generation": accepted_revision.revision_number,
        "composition_digest": _required(composition.content_digest),
        "harness_digest": harness_digest,
        "effective_policy_digest": evaluation.effective_policy_digest,
        "operator_config_digest": operator_config_digest,
        "repository": run.repository,
        "parent_evaluation_digest": _required(evaluation.content_digest),
        "goal_evaluator_digest": evaluation.goal_evaluator_digest,
        "verification_evidence_digests": evaluation.verification_result_digests,
        "evaluation_ledger_digests": evaluation.evaluation_ledger_digests,
        "semantic_evidence_digests": semantic_evidence_digests,
        "node_fact_digests": node_fact_digests,
        "changed_paths": paths,
        "maximum_node_risk": maximum_risk,
        "changed_files": len(paths),
        "patch_bytes": 0
        if composition.candidate_patch is None
        else composition.candidate_patch.size_bytes,
        "rule_config_digest": rule_config_digest,
    }
    approval_expected = {
        "authorization_digest": authority.content_digest,
        "rule_id": authority.rule_id,
        "reason_code": authority.reason_code,
        "accepted_graph_revision_digest": authority.accepted_graph_revision_digest,
        "harness_digest": authority.harness_digest,
        "operator_config_digest": authority.operator_config_digest,
        "parent_evaluation_digest": authority.parent_evaluation_digest,
        "verification_evidence_digests": authority.verification_evidence_digests,
        "evaluation_evidence_digests": authority.evaluation_ledger_digests,
        "semantic_evidence_digests": authority.semantic_evidence_digests,
    }
    if (
        authority.decision != "policy_auto_approved"
        or authority.mode != "policy"
        or authority.rule_id != PROMOTION_AUTO_APPROVAL_RULE_ID
        or authority.reason_code != "eligible_low_risk_exact_evidence"
        or any(getattr(authority, name) != value for name, value in expected.items())
        or approval.authorization_kind != "policy_auto"
        or approval.decision != "approved"
        or approval.request_digest != authority.candidate_digest
        or approval.policy_digest != authority.effective_policy_digest
        or any(getattr(approval, name) != value for name, value in approval_expected.items())
        or policy.mode != "policy"
        or harness.approvals.promotion != "policy"
        or harness.provisional
        or run.harness_digest != harness_digest
        or run.operator_config_digest != operator_config_digest
        or run.accepted_graph_revision_digest != accepted_revision.content_digest
        or run.parent_evaluation_digest != evaluation.content_digest
        or run.parent_candidate_digest != evaluation.candidate_artifact_digest
        or run.repository is None
        or str(Path(run.repository).resolve())
        not in {str(Path(item).resolve()) for item in policy.allowed_repositories}
        or maximum_risk > policy.max_risk
        or harness.network.mode.value != "disabled"
        or bool(harness.install.ecosystems)
        or bool(capabilities & {"download", "install"})
        or not paths
        or composition.candidate_patch is None
        or len(paths) > policy.max_changed_files
        or composition.candidate_patch.size_bytes > policy.max_patch_bytes
        or any(_matches(path, pattern) for path in paths for pattern in denied_patterns)
    ):
        raise ValueError("policy auto-approval authority is stale or no longer eligible")


def _required(value: str | None) -> str:
    if value is None:
        raise ValueError("promotion authority is missing an exact digest")
    return value
