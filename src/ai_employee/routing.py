"""Fixed, policy, and transparent history-aware adaptive routing."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from .domain import ExecutionStrategy, RoutingMode, StrategyPerformance

MIN_ADAPTIVE_SAMPLES = 3


class RoutingError(ValueError):
    pass


def select_strategy(
    strategies: Iterable[ExecutionStrategy],
    *,
    mode: RoutingMode,
    required_capabilities: Iterable[str] = (),
    strategy_capabilities: Mapping[str, Iterable[str]] | None = None,
    performances: Iterable[StrategyPerformance] = (),
    fixed_strategy_id: str | None = None,
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
    if not eligible:
        raise RoutingError("no strategy satisfies mandatory project and safety constraints")
    if mode is RoutingMode.FIXED:
        selected_id = fixed_strategy_id or eligible[0].id
        selected = next((item for item in eligible if item.id == selected_id), None)
        if selected is None:
            raise RoutingError("fixed strategy is unavailable or violates policy")
        return selected.model_copy(update={"routing_reasons": ("fixed strategy selected",)})
    if mode is RoutingMode.POLICY:
        return eligible[0].model_copy(
            update={"routing_reasons": ("first policy-eligible strategy",)}
        )

    history = {item.strategy_id: item for item in performances}
    mature = [
        item
        for item in eligible
        if history.get(item.id, _empty(item.id)).sample_count >= MIN_ADAPTIVE_SAMPLES
    ]
    if not mature:
        return eligible[0].model_copy(
            update={"routing_reasons": ("insufficient history; deterministic policy fallback",)}
        )
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
    return winner.model_copy(
        update={
            "routing_reasons": (
                f"adaptive history samples={performance.sample_count}",
                f"success_rate={_success_rate(performance):.3f}",
                "mandatory constraints applied before optimization",
            )
        }
    )


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
