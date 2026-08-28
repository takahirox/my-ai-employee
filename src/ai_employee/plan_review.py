"""Strict, non-authoritative plan-review contracts and deterministic action rule."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Literal

from pydantic import Field, field_validator

from .domain import ExecutionStrategy, Goal
from .domain.base import Digest, Identifier, StableStrEnum
from .domain.v2 import DigestedRecordV2, SchemaModelV2
from .serialization import canonical_digest, canonical_json
from .task_planning import ProposedGraph


class PlanReviewFindingType(StableStrEnum):
    MISSING_GOAL_COVERAGE = "missing_goal_coverage"
    UNNECESSARY_TASK = "unnecessary_task"
    SCOPE_EXPANSION = "scope_expansion"
    PREMATURE_GENERALIZATION = "premature_generalization"
    OVER_FRAGMENTATION = "over_fragmentation"
    UNDER_DECOMPOSITION = "under_decomposition"
    UNNECESSARY_REFACTOR = "unnecessary_refactor"
    VERIFICATION_GAP = "verification_gap"
    UNCLEAR_GOAL_TRACEABILITY = "unclear_goal_traceability"


class PlanReviewImpact(StableStrEnum):
    BLOCKING = "blocking"
    ADVISORY = "advisory"


class PlanReviewAction(StableStrEnum):
    ACCEPT = "accept"
    REQUEST_REVISION = "request_revision"
    REJECT = "reject"


class PlanReviewFinding(SchemaModelV2):
    """One bounded reviewer claim; it has no graph or execution authority."""

    schema_name: ClassVar[str] = "plan_review_finding"
    id: Identifier
    finding_type: PlanReviewFindingType
    impact: PlanReviewImpact
    affected_node_ids: tuple[Identifier, ...] = Field(default=(), max_length=16)
    goal_relation: str = Field(min_length=1, max_length=1_000)
    smallest_correction: str = Field(min_length=1, max_length=1_000)

    @field_validator("goal_relation", "smallest_correction")
    @classmethod
    def _non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("plan-review text must be non-blank")
        return value

    @field_validator("affected_node_ids")
    @classmethod
    def _canonical_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("plan-review references must be unique")
        if value != tuple(sorted(value)):
            raise ValueError("plan-review references must be deterministically ordered")
        return value


class PlanReviewPayload(SchemaModelV2):
    """The complete model-controlled plan-review output."""

    schema_name: ClassVar[str] = "plan_review_payload"
    findings: tuple[PlanReviewFinding, ...] = Field(default=(), max_length=16)

    @field_validator("findings")
    @classmethod
    def _canonical_findings(
        cls, value: tuple[PlanReviewFinding, ...]
    ) -> tuple[PlanReviewFinding, ...]:
        ids = tuple(finding.id for finding in value)
        if len(ids) != len(set(ids)):
            raise ValueError("plan-review finding IDs must be unique")
        if ids != tuple(sorted(ids)):
            raise ValueError("plan-review findings must be deterministically ordered")
        return value


@dataclass(frozen=True, order=True)
class PlanReviewValidationIssue:
    code: str
    subject_id: str
    message: str


class PlanReviewValidationError(ValueError):
    def __init__(self, issues: tuple[PlanReviewValidationIssue, ...]) -> None:
        self.issues = tuple(sorted(set(issues)))
        super().__init__("; ".join(f"{issue.code}:{issue.subject_id}" for issue in self.issues))


class TrustedPlanReview(DigestedRecordV2):
    """Immutable Trust Kernel binding of review evidence to exact accepted inputs."""

    schema_name: ClassVar[str] = "trusted_plan_review"
    goal_id: Identifier
    goal_digest: Digest
    proposed_graph_id: Identifier
    proposed_graph_digest: Digest
    review_round: Literal[0, 1]
    reviewer_strategy: ExecutionStrategy
    effective_policy_digest: Digest
    harness_digest: Digest
    findings: tuple[PlanReviewFinding, ...] = Field(max_length=16)


def validate_plan_review(
    payload: PlanReviewPayload,
    *,
    goal: Goal,
    proposed_graph: ProposedGraph,
) -> tuple[PlanReviewValidationIssue, ...]:
    """Validate all cross-record references without trusting reviewer-authored bindings."""

    issues: list[PlanReviewValidationIssue] = []
    if proposed_graph.goal_id != goal.id:
        issues.append(
            PlanReviewValidationIssue(
                "goal_id_mismatch",
                proposed_graph.id,
                "proposed graph is bound to another goal",
            )
        )
    expected_goal_digest = canonical_digest(goal)
    if proposed_graph.goal_digest != expected_goal_digest:
        issues.append(
            PlanReviewValidationIssue(
                "goal_digest_mismatch",
                proposed_graph.id,
                "proposed graph is not bound to the canonical goal content",
            )
        )

    node_ids = {node.id for node in proposed_graph.graph.nodes}
    for finding in payload.findings:
        for node_id in finding.affected_node_ids:
            if node_id not in node_ids:
                issues.append(
                    PlanReviewValidationIssue(
                        "unknown_node_reference",
                        finding.id,
                        f"unknown affected node {node_id!r}",
                    )
                )
    return tuple(sorted(set(issues)))


def bind_plan_review(
    payload: PlanReviewPayload,
    *,
    record_id: Identifier,
    run_id: Identifier,
    created_at: datetime,
    review_round: Literal[0, 1],
    goal: Goal,
    proposed_graph: ProposedGraph,
    reviewer_strategy: ExecutionStrategy,
) -> TrustedPlanReview:
    """Validate, snapshot, and bind one review to Trust Kernel supplied facts."""

    issues = validate_plan_review(payload, goal=goal, proposed_graph=proposed_graph)
    if issues:
        raise PlanReviewValidationError(issues)
    proposal_digest = proposed_graph.content_digest
    if proposal_digest is None:  # defensive: DigestedRecordV2 normally binds this
        raise ValueError("ProposedGraph is missing its canonical content digest")
    snapshot = PlanReviewPayload.model_validate_json(canonical_json(payload), strict=True)
    return TrustedPlanReview(
        id=record_id,
        run_id=run_id,
        created_at=created_at,
        goal_id=goal.id,
        goal_digest=canonical_digest(goal),
        proposed_graph_id=proposed_graph.id,
        proposed_graph_digest=proposal_digest,
        review_round=review_round,
        reviewer_strategy=reviewer_strategy,
        effective_policy_digest=proposed_graph.effective_policy_digest,
        harness_digest=proposed_graph.harness_digest,
        findings=snapshot.findings,
    )


def decide_plan_review_action(review: TrustedPlanReview) -> PlanReviewAction:
    """Apply the complete bounded first-milestone round rule."""

    if not any(finding.impact is PlanReviewImpact.BLOCKING for finding in review.findings):
        return PlanReviewAction.ACCEPT
    if review.review_round == 0:
        return PlanReviewAction.REQUEST_REVISION
    return PlanReviewAction.REJECT
