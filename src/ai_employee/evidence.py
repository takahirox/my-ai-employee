"""Deterministic evidence, completion, risk, and merge assessments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .domain import (
    Artifact, CompletionCriterion, EvidenceCoverage, EvidencePack, Finding, MergeDecision,
    MergeDecisionState, Reference, ReviewAssessment, VerificationEvidence,
    VerificationRequirement,
)


def aggregate_coverage(
    requirements: Iterable[VerificationRequirement],
    evidence: Iterable[VerificationEvidence],
) -> EvidenceCoverage:
    requirement_list = tuple(requirements)
    by_id = {item.id: item for item in requirement_list}
    mapping: dict[str, list[str]] = {item.id: [] for item in requirement_list}
    for item in sorted(evidence, key=lambda value: value.id):
        if not item.passed:
            continue
        for requirement_id in sorted(set(item.requirement_ids)):
            requirement = by_id.get(requirement_id)
            if requirement is not None and item.kind in requirement.accepted_evidence_kinds:
                mapping[requirement_id].append(item.id)
    satisfied = tuple(sorted(key for key, values in mapping.items() if values))
    missing = tuple(sorted(set(by_id) - set(satisfied)))
    return EvidenceCoverage(
        requirement_ids=tuple(sorted(by_id)), satisfied_requirement_ids=satisfied,
        missing_requirement_ids=missing,
        mapping={key: sorted(values) for key, values in sorted(mapping.items())}, complete=not missing,
    )


def build_evidence_pack(
    *, pack_id: str, run_id: str, contract_ids: Iterable[str],
    requirements: Iterable[VerificationRequirement], evidence: Iterable[VerificationEvidence],
    reviews: Iterable[ReviewAssessment] = (), artifact_refs: Iterable[Reference] = (),
) -> EvidencePack:
    requirement_items = tuple(requirements)
    evidence_items = tuple(evidence)
    return EvidencePack(
        id=pack_id, run_id=run_id, contract_ids=tuple(sorted(set(contract_ids))),
        requirements=requirement_items, evidence=evidence_items,
        coverage=aggregate_coverage(requirement_items, evidence_items), reviews=tuple(reviews),
        artifact_refs=tuple(artifact_refs), created_at=datetime.now(timezone.utc),
    )


@dataclass(frozen=True)
class CompletionAssessment:
    complete: bool
    reasons: tuple[str, ...]


def assess_completion(
    *, criteria: Iterable[CompletionCriterion], coverage: EvidenceCoverage,
    artifacts: Iterable[Artifact], mandatory_gates_passed: bool,
    terminal_nodes_succeeded: bool = True,
    findings: Iterable[Finding] = (),
) -> CompletionAssessment:
    artifact_ids = {item.id for item in artifacts}
    satisfied = set(coverage.satisfied_requirement_ids)
    reasons: list[str] = []
    for criterion in sorted(criteria, key=lambda item: item.id):
        if criterion.mandatory:
            for missing in sorted(set(criterion.verification_requirement_ids) - satisfied):
                reasons.append(f"criterion {criterion.id} missing evidence: {missing}")
            for missing in sorted(set(criterion.required_artifact_ids) - artifact_ids):
                reasons.append(f"criterion {criterion.id} missing artifact: {missing}")
    if not mandatory_gates_passed:
        reasons.append("mandatory gates have not passed")
    if not terminal_nodes_succeeded:
        reasons.append("terminal nodes have not succeeded")
    reasons.extend(
        f"blocking finding remains: {item.id}"
        for item in sorted(findings, key=lambda value: value.id) if item.blocking
    )
    return CompletionAssessment(not reasons, tuple(reasons))


def decide_merge(
    evidence_pack: EvidencePack, *, mandatory_approval_required: bool,
    mandatory_approval_satisfied: bool,
) -> MergeDecision:
    blocking = sorted(
        (finding for review in evidence_pack.reviews for finding in review.blocking_findings if finding.blocking),
        key=lambda item: item.id,
    )
    if blocking:
        state = MergeDecisionState.CHANGES_REQUIRED
        reasons = tuple(f"blocking finding: {item.id}" for item in blocking)
    elif not evidence_pack.coverage.complete:
        state = MergeDecisionState.MORE_EVIDENCE_REQUIRED
        reasons = tuple(f"missing evidence: {item}" for item in evidence_pack.coverage.missing_requirement_ids)
    elif mandatory_approval_required and not mandatory_approval_satisfied:
        state = MergeDecisionState.HUMAN_REVIEW_REQUIRED
        reasons = ("mandatory approval remains outstanding",)
    elif any(not review.approved for review in evidence_pack.reviews):
        state = MergeDecisionState.REJECTED
        reasons = ("a structured review rejected the candidate",)
    else:
        state = MergeDecisionState.AUTO_MERGE_ELIGIBLE
        reasons = ("all mandatory evidence and review gates are satisfied",)
    return MergeDecision(
        id=f"merge-{evidence_pack.id}", state=state, reasons=reasons,
        evidence_pack_id=evidence_pack.id,
        mandatory_approval_satisfied=mandatory_approval_satisfied,
    )


@dataclass(frozen=True)
class RiskAssessment:
    level: str
    factors: tuple[str, ...]


def assess_change_risk(*, changed_paths: Iterable[str], touches_policy: bool = False) -> RiskAssessment:
    paths = tuple(sorted(set(changed_paths)))
    factors: list[str] = []
    if len(paths) > 20:
        factors.append("more than 20 paths changed")
    if any(path.startswith(("src/", "lib/")) for path in paths):
        factors.append("runtime source changed")
    if any("migration" in path.lower() for path in paths):
        factors.append("persistence migration changed")
    if touches_policy:
        factors.append("execution policy changed")
    level = "high" if len(factors) >= 3 else "medium" if factors else "low"
    return RiskAssessment(level, tuple(factors or ["documentation or metadata only"]))
