"""Deterministic, fail-closed authority for low-risk graph promotion approval."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import Field, model_validator

from .config import OperatorConfig, PromotionAutoApprovalConfig
from .domain import ProjectHarnessV2
from .domain.base import Digest, Identifier
from .domain.evaluation import EvaluationDecision
from .domain.models import AcceptedGraphRevision
from .domain.v2 import ApprovalRecord, DigestedRecordV2
from .graph_composition import GraphPatchCompositionRecord
from .graph_evaluation import (
    ParentCandidateEvaluationRecord,
    ParentCandidateEvaluationReplay,
)
from .serialization import canonical_digest
from .task_orchestration import GraphRunRecord

PROMOTION_AUTO_APPROVAL_RULE_ID = "low-risk-exact-evidence-v1"

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
    ) -> Identifier:
        if self.operator_policy.mode != "policy":
            return "manual_mode_default"
        if self.harness.approvals.promotion != "policy":
            return "project_policy_opt_in_missing"
        if replay is None:
            return "evidence_replay_unavailable"
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
