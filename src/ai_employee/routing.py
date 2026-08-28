"""Fixed, policy, and transparent history-aware adaptive routing."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from unicodedata import normalize

from .domain import (
    ExecutionStrategy,
    RoutingMode,
    SemanticTaskAssessment,
    StrategyPerformance,
    TaskAssessment,
    TaskDecompositionItem,
)
from .serialization import canonical_digest

MIN_ADAPTIVE_SAMPLES = 3


class RoutingError(ValueError):
    pass


def merge_semantic_assessment(
    deterministic: TaskAssessment,
    semantic: SemanticTaskAssessment,
    *,
    available_capabilities: Iterable[str],
) -> TaskAssessment:
    """Merge semantic classification without allowing it to weaken hard facts."""

    available = set(available_capabilities)
    unknown = set(semantic.required_capabilities) - available
    if unknown:
        raise RoutingError(
            f"semantic assessment returned unsupported capabilities: {sorted(unknown)}"
        )
    required = tuple(
        dict.fromkeys((*deterministic.required_capabilities, *semantic.required_capabilities))
    )
    reasons = tuple(
        dict.fromkeys(
            (
                *deterministic.reasons,
                *(f"semantic assessment: {reason}" for reason in semantic.reasons),
            )
        )
    )
    if len(reasons) > 20:
        raise RoutingError("combined assessment has too many reasons")
    return TaskAssessment(
        id=deterministic.id,
        run_id=deterministic.run_id,
        goal_digest=deterministic.goal_digest,
        complexity=max(deterministic.complexity, semantic.complexity),
        scale=max(deterministic.scale, semantic.scale),
        risk=deterministic.risk,
        required_capabilities=required,
        decomposition=deterministic.decomposition,
        reasons=reasons,
    )


def assess_task(
    goal: str,
    *,
    run_id: str,
    risk: int = 0,
    required_capabilities: Iterable[str] = (),
) -> TaskAssessment:
    """Build a deterministic, bounded assessment without granting execution authority."""

    normalized_goal = normalize("NFKC", goal).strip()
    if not normalized_goal:
        raise RoutingError("goal must not be blank")
    if len(normalized_goal) > 10_000:
        raise RoutingError("goal must be at most 10000 characters after normalization")
    if not 0 <= risk <= 10:
        raise RoutingError("risk must be between 0 and 10")

    capabilities = tuple(required_capabilities)
    if len(capabilities) > 50:
        raise RoutingError("at most 50 required capabilities may be assessed")

    segments = tuple(
        segment.strip()
        for segment in re.split(r"[;\r\n\u0085\u2028\u2029]+", normalized_goal)
        if segment.strip()
    )[:20]
    if not segments:
        segments = (normalized_goal,)
    item_count = len(segments)
    capability_count = len(capabilities)
    complexity = min(
        10,
        1 + len(normalized_goal) // 1_000 + capability_count + (item_count - 1) // 5,
    )
    scale = min(10, item_count)
    goal_digest = canonical_digest(normalized_goal)

    items = tuple(
        TaskDecompositionItem(
            id=f"assessment-item.{canonical_digest((goal_digest, position, segment))}",
            title=segment[:500],
            complexity=min(10, 1 + len(segment) // 1_000 + capability_count),
            scale=1,
            risk=risk,
            required_capabilities=capabilities,
            reasons=(
                (
                    f"complexity from segment_length={len(segment)} "
                    f"and capability_count={capability_count}"
                ),
                "scale=1 for one structural item; assessment only",
            ),
        )
        for position, segment in enumerate(segments, start=1)
    )
    reasons = (
        (
            f"complexity={complexity} from goal_length={len(normalized_goal)}, "
            f"item_count={item_count}, and capability_count={capability_count}"
        ),
        f"scale={scale} from structural_item_count={item_count}",
        f"risk={risk} preserved from caller input",
    )
    identity = {
        "run_id": run_id,
        "goal_digest": goal_digest,
        "risk": risk,
        "required_capabilities": capabilities,
    }
    return TaskAssessment(
        id=f"assessment.{canonical_digest(identity)}",
        run_id=run_id,
        goal_digest=goal_digest,
        complexity=complexity,
        scale=scale,
        risk=risk,
        required_capabilities=capabilities,
        decomposition=items,
        reasons=reasons,
    )


def select_strategy(
    strategies: Iterable[ExecutionStrategy],
    *,
    mode: RoutingMode,
    required_capabilities: Iterable[str] = (),
    strategy_capabilities: Mapping[str, Iterable[str]] | None = None,
    performances: Iterable[StrategyPerformance] = (),
    fixed_strategy_id: str | None = None,
    assessment: TaskAssessment | None = None,
    allowed_strategy_ids: Iterable[str] = (),
    allowed_backends: Iterable[str] = (),
    local_backend_allowed: bool = False,
) -> ExecutionStrategy:
    """Select under mandatory constraints first, then transparent heuristics."""

    candidates = tuple(sorted(strategies, key=lambda item: item.id))
    required = set(required_capabilities)
    capability_map = strategy_capabilities or {}
    eligible = (
        tuple(item for item in candidates if required <= set(capability_map.get(item.id, ())))
        if required
        else candidates
    )
    if assessment is not None:
        allowed_ids = set(allowed_strategy_ids)
        allowed_backend_names = set(allowed_backends)
        assessed_required = set(assessment.required_capabilities)
        eligible = tuple(
            item
            for item in eligible
            if item.id in allowed_ids
            and item.backend in allowed_backend_names
            and (item.backend not in {"ollama", "ollama_cli"} or local_backend_allowed)
            and assessed_required <= set(item.capabilities)
            and item.min_complexity <= assessment.complexity <= item.max_complexity
            and item.min_scale <= assessment.scale <= item.max_scale
            and assessment.risk <= item.max_risk
        )
    if not eligible:
        raise RoutingError("no strategy satisfies mandatory project and safety constraints")
    if mode is RoutingMode.FIXED:
        selected_id = (
            fixed_strategy_id if assessment is not None else fixed_strategy_id or eligible[0].id
        )
        selected = next((item for item in eligible if item.id == selected_id), None)
        if selected is None:
            raise RoutingError("fixed strategy is unavailable or violates policy")
        fixed_reasons: tuple[str, ...] = ("fixed strategy selected",)
        if assessment is not None:
            fixed_reasons += ("assessment authority and suitability satisfied",)
        return selected.model_copy(update={"routing_reasons": fixed_reasons})

    fit_reasons: tuple[str, ...] = ()
    if assessment is not None:
        best_headroom = min(_assessment_headroom(item, assessment) for item in eligible)
        eligible = tuple(
            item for item in eligible if _assessment_headroom(item, assessment) == best_headroom
        )
        fit_reasons = (f"assessment headroom={best_headroom}",)
    if mode is RoutingMode.POLICY:
        policy_reasons: tuple[str, ...] = ("first policy-eligible strategy", *fit_reasons)
        return eligible[0].model_copy(update={"routing_reasons": policy_reasons})

    history = {item.strategy_id: item for item in performances}
    mature = [
        item
        for item in eligible
        if history.get(item.id, _empty(item.id)).sample_count >= MIN_ADAPTIVE_SAMPLES
    ]
    if not mature:
        fallback_reasons: tuple[str, ...] = (
            "insufficient history; deterministic policy fallback",
            *fit_reasons,
        )
        return eligible[0].model_copy(update={"routing_reasons": fallback_reasons})
    ranked = sorted(
        mature,
        key=lambda item: (
            -_success_rate(history[item.id]),
            _average_duration(history[item.id]),
            _average_cost(history[item.id]),
            item.id,
        ),
    )
    winner = ranked[0]
    performance = history[winner.id]
    adaptive_reasons: tuple[str, ...] = (
        f"adaptive history samples={performance.sample_count}",
        f"success_rate={_success_rate(performance):.3f}",
        "mandatory constraints applied before optimization",
        *fit_reasons,
    )
    return winner.model_copy(update={"routing_reasons": adaptive_reasons})


def record_outcome(
    performance: StrategyPerformance | None,
    *,
    strategy_id: str,
    succeeded: bool,
    duration_seconds: float,
    cost: float,
) -> StrategyPerformance:
    current = performance or _empty(strategy_id)
    return StrategyPerformance(
        id=current.id,
        strategy_id=strategy_id,
        sample_count=current.sample_count + 1,
        success_count=current.success_count + int(succeeded),
        total_duration_seconds=current.total_duration_seconds + duration_seconds,
        total_cost=current.total_cost + cost,
        updated_at=datetime.now(UTC),
    )


def _assessment_headroom(
    strategy: ExecutionStrategy,
    assessment: TaskAssessment,
) -> int:
    return (
        strategy.max_complexity
        - assessment.complexity
        + strategy.max_scale
        - assessment.scale
        + strategy.max_risk
        - assessment.risk
    )


def _empty(strategy_id: str) -> StrategyPerformance:
    return StrategyPerformance(
        id=f"performance-{strategy_id}",
        strategy_id=strategy_id,
        updated_at=datetime.now(UTC),
    )


def _success_rate(item: StrategyPerformance) -> float:
    return item.success_count / item.sample_count if item.sample_count else 0.0


def _average_duration(item: StrategyPerformance) -> float:
    return item.total_duration_seconds / item.sample_count if item.sample_count else float("inf")


def _average_cost(item: StrategyPerformance) -> float:
    return item.total_cost / item.sample_count if item.sample_count else float("inf")
