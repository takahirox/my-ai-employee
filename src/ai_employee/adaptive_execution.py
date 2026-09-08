"""Non-authoritative recommendations and immutable adaptive execution choices."""

from __future__ import annotations

from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain import ExecutionStrategy, SemanticTaskProfile, TaskAssessment
from .domain.base import Digest
from .domain.v2 import DigestedRecordV2
from .serialization import canonical_digest


class ExecutionRecommendation(BaseModel):
    """Work-shape advice; never a permission, strategy or verification decision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: Literal["direct", "planned", "unknown"]
    scope_clear: bool
    criteria_clear: bool
    coordinated_work_required: bool
    planning_requested: bool
    reason: str = Field(min_length=1, max_length=500)


class EffectAwareExecutionRecommendation(ExecutionRecommendation):
    """Scope of intended effects, including effects of deferred scripts or actions."""

    effect_scope: Literal[
        "read_only_or_local_reversible", "external_or_protected_change", "unknown"
    ]


class GoalAssessmentPayload(SemanticTaskProfile):
    execution_recommendation: EffectAwareExecutionRecommendation | None = None


def choose_adaptive_path(
    assessment: TaskAssessment,
    recommendation: ExecutionRecommendation | None,
    *,
    plan_review_required: bool,
    planning_requested: bool,
    has_completion_criteria: bool,
) -> tuple[Literal["direct", "planned"], str]:
    if plan_review_required:
        return "planned", "Harness requires plan review."
    if planning_requested:
        return "planned", "Operator explicitly requested planning."
    if not has_completion_criteria:
        return "planned", "Accepted completion criteria are missing."
    if recommendation is None or recommendation.path != "direct":
        return "planned", "No validated direct-execution recommendation."
    if (
        not isinstance(recommendation, EffectAwareExecutionRecommendation)
        or recommendation.effect_scope != "read_only_or_local_reversible"
    ):
        return "planned", "Consequential or unknown effects require the planned path."
    if (
        not recommendation.scope_clear
        or not recommendation.criteria_clear
        or recommendation.coordinated_work_required
        or recommendation.planning_requested
    ):
        return "planned", "Assessment requires scope clarification, coordination or planning."
    profile = assessment.semantic_profile
    if (
        profile is None
        or profile.ambiguity != "low"
        or profile.scope not in {"bounded", "local"}
        or profile.reasoning_class not in {"mechanical", "simple", "moderate"}
        or profile.task_type not in {"mechanical", "retrieval", "diagnosis", "implementation"}
    ):
        return "planned", "Semantic work shape does not support direct execution."
    return "direct", "One worker can address the accepted bounded scope and completion criteria."


class AdaptiveExecutionDecision(DigestedRecordV2):
    schema_name: ClassVar[str] = "adaptive_execution_decision"

    execution_profile_digest: Digest
    goal_digest: Digest
    assessment: TaskAssessment
    assessment_digest: Digest
    assessment_strategy: ExecutionStrategy
    # Keep the legacy shape intact so already accepted decisions retain their digest.
    recommendation: EffectAwareExecutionRecommendation | ExecutionRecommendation | None
    path: Literal["direct", "planned"]
    reason: str = Field(min_length=1, max_length=1_000)
    initial_worker_strategy: ExecutionStrategy
    harness_digest: Digest
    operator_config_digest: Digest
    effective_policy_digest: Digest
    direct_graph_digest: Digest | None
    continuation: str = (
        "Preserve findings, artifacts, completed operations and the original outcome. "
        "If accepted scope or criteria are insufficient, use an explicit planned continuation "
        "with reconciled prior work; do not restart side effects or grant new authority. "
        "Any supported in-run transition shares the remaining run allowance."
    )

    @model_validator(mode="after")
    def _bound_assessment(self) -> Self:
        if self.assessment.run_id != self.run_id:
            raise ValueError("adaptive decision assessment belongs to another run")
        if self.assessment_digest != canonical_digest(self.assessment):
            raise ValueError("adaptive decision assessment digest is stale")
        if (self.path == "direct") != (self.direct_graph_digest is not None):
            raise ValueError("only direct execution binds a deterministic graph")
        return self
