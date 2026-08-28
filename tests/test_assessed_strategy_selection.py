from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ai_employee.domain import (
    ExecutionStrategy,
    RoutingMode,
    SemanticAmbiguity,
    SemanticReasoningClass,
    SemanticScope,
    SemanticTaskProfile,
    SemanticTaskType,
    StrategyPerformance,
    TaskAssessment,
)
from ai_employee.routing import RoutingError, assess_task, merge_semantic_profile, select_strategy


def _assessment(
    complexity: int = 2,
    risk: int = 0,
    capabilities: tuple[str, ...] = (),
) -> TaskAssessment:
    return TaskAssessment(
        id="assessment.test",
        run_id="run.test",
        goal_digest="a" * 64,
        complexity=complexity,
        scale=1,
        risk=risk,
        required_capabilities=capabilities,
        reasons=("focused routing fixture",),
    )


def _strategy(
    strategy_id: str,
    *,
    backend: str = "codex_cli",
    capabilities: tuple[str, ...] = ("code",),
    max_complexity: int = 10,
    max_risk: int = 10,
) -> ExecutionStrategy:
    return ExecutionStrategy(
        id=strategy_id,
        routing_mode=RoutingMode.POLICY,
        backend=backend,
        model=strategy_id,
        capabilities=capabilities,
        max_complexity=max_complexity,
        max_risk=max_risk,
    )


def _performance(strategy_id: str, successes: int) -> StrategyPerformance:
    return StrategyPerformance(
        id=f"performance.{strategy_id}",
        strategy_id=strategy_id,
        sample_count=3,
        success_count=successes,
        total_duration_seconds=3.0,
        total_cost=0.3,
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


def _select(
    strategies: tuple[ExecutionStrategy, ...],
    assessment: TaskAssessment,
    *,
    mode: RoutingMode = RoutingMode.POLICY,
    ids: tuple[str, ...] | None = None,
    backends: tuple[str, ...] = ("codex_cli",),
    local: bool = False,
    fixed: str | None = None,
    history: tuple[StrategyPerformance, ...] = (),
) -> ExecutionStrategy:
    return select_strategy(
        strategies,
        mode=mode,
        assessment=assessment,
        allowed_strategy_ids=ids or tuple(item.id for item in strategies),
        allowed_backends=backends,
        local_backend_allowed=local,
        fixed_strategy_id=fixed,
        performances=history,
    )


def test_assessment_fit_chooses_smallest_adequate_strategy() -> None:
    small = _strategy("strategy.small", max_complexity=3, max_risk=2)
    large = _strategy("strategy.large")
    strategies = (large, small)

    selected = _select(strategies, _assessment())

    assert selected.id == "strategy.small"
    assert any("assessment headroom=" in reason for reason in selected.routing_reasons)
    for complexity, risk in ((8, 0), (2, 6)):
        assert _select(strategies, _assessment(complexity, risk)).id == "strategy.large"


def test_short_open_ended_profile_selects_the_stronger_eligible_strategy() -> None:
    profile = SemanticTaskProfile(
        task_type=SemanticTaskType.OPEN_ENDED_STRATEGY,
        reasoning_class=SemanticReasoningClass.DEEP,
        scope=SemanticScope.BOUNDED,
        ambiguity=SemanticAmbiguity.LOW,
        reasons=("the success path is intentionally open",),
    )
    assessment = merge_semantic_profile(assess_task("Choose direction", run_id="run.open"), profile)
    small = _strategy("strategy.small", max_complexity=3)
    strong = _strategy("strategy.strong")

    assert assessment.context_character_count == len("Choose direction")
    assert assessment.complexity == 9
    assert _select((small, strong), assessment).id == "strategy.strong"


def test_authority_and_capabilities_are_an_exact_intersection() -> None:
    match = _strategy("strategy.match")
    wrong_id = _strategy("strategy.wrong-id")
    wrong_backend = _strategy("strategy.wrong-backend", backend="claude_code_cli")
    missing_capability = _strategy("strategy.missing-capability", capabilities=())
    strategies = (match, wrong_id, wrong_backend, missing_capability)
    assessment = _assessment(capabilities=("code",))
    allowed = ("strategy.match", "strategy.wrong-backend", "strategy.missing-capability")

    selected = _select(strategies, assessment, ids=allowed)

    assert selected.id == "strategy.match"
    assert "first policy-eligible strategy" in selected.routing_reasons
    with pytest.raises(RoutingError, match="no strategy satisfies mandatory"):
        _select(strategies, assessment, ids=allowed[1:])


def test_ollama_cli_requires_explicit_local_authority() -> None:
    local = _strategy("strategy.local", backend="ollama_cli")

    with pytest.raises(RoutingError, match="no strategy satisfies mandatory"):
        _select((local,), _assessment(), backends=("ollama_cli",))

    selected = _select((local,), _assessment(), backends=("ollama_cli",), local=True)
    assert selected.id == "strategy.local"
    assert selected.routing_reasons


def test_assessed_fixed_mode_requires_the_exact_eligible_id() -> None:
    tight = _strategy("strategy.tight", max_complexity=2)
    large = _strategy("strategy.large")
    assessment = _assessment(complexity=8)

    selected = _select((tight, large), assessment, mode=RoutingMode.FIXED, fixed="strategy.large")

    assert selected.id == "strategy.large"
    assert "assessment authority and suitability satisfied" in selected.routing_reasons
    with pytest.raises(RoutingError, match="fixed strategy is unavailable"):
        _select(
            (tight, large),
            assessment,
            mode=RoutingMode.FIXED,
            fixed="strategy.tight",
        )


def test_history_only_breaks_equal_fit_ties() -> None:
    tight = _strategy("strategy.tight", max_complexity=3, max_risk=2)
    roomy = _strategy("strategy.roomy")
    assessment = _assessment()
    selected = _select(
        (roomy, tight),
        assessment,
        mode=RoutingMode.ADAPTIVE,
        history=(_performance("strategy.roomy", 3),),
    )

    assert selected.id == "strategy.tight"
    assert any("assessment headroom=" in reason for reason in selected.routing_reasons)

    equal_a = _strategy("strategy.equal-a", max_complexity=3, max_risk=2)
    equal_b = _strategy("strategy.equal-b", max_complexity=3, max_risk=2)
    selected = _select(
        (equal_a, equal_b),
        assessment,
        mode=RoutingMode.ADAPTIVE,
        history=(
            _performance("strategy.equal-a", 0),
            _performance("strategy.equal-b", 3),
        ),
    )

    assert selected.id == "strategy.equal-b"
    assert "adaptive history samples=3" in selected.routing_reasons
