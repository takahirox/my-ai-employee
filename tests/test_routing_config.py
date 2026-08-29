from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_employee.config import (
    OperatorConfig,
    OperatorRoutingConfig,
    OperatorStrategyConfig,
)
from ai_employee.domain import (
    ExecutionStrategy,
    RoutingMode,
    SemanticAmbiguity,
    SemanticReasoningClass,
    SemanticScope,
    SemanticTaskProfile,
    SemanticTaskType,
)
from ai_employee.routing import assess_task, merge_semantic_profile, select_strategy
from ai_employee.serialization import canonical_digest, operator_config_digest


def test_config_without_routing_uses_builtin_codex_balanced_default() -> None:
    config = OperatorConfig.model_validate({"schema_version": 1, "workers": {}}, strict=True)

    assert config.strategy_set_name() == "codex-balanced"
    assessor = config.assessment_strategy(RoutingMode.ADAPTIVE)
    assert (assessor.id, assessor.model, assessor.effort) == (
        "codex-sol-high",
        "gpt-5.6-sol",
        "high",
    )
    strategies = config.execution_strategies(RoutingMode.ADAPTIVE)
    assert tuple((strategy.id, strategy.model, strategy.effort) for strategy in strategies) == (
        ("codex-luna-max", "gpt-5.6-luna", "max"),
        ("codex-sol-high", "gpt-5.6-sol", "high"),
    )
    assert strategies[0].max_complexity == 3
    assert strategies[0].max_scale == 3
    assert strategies[0].max_risk == 0
    assert config.planner_strategies(RoutingMode.ADAPTIVE) == strategies

    claude = config.execution_strategies(RoutingMode.ADAPTIVE, "claude-only")
    assert tuple((strategy.id, strategy.model, strategy.effort) for strategy in claude) == (
        ("claude-opus-high", "claude-opus-5", "high"),
        ("claude-fable-high", "claude-fable-5", "high"),
    )
    claude_assessor = config.assessment_strategy(RoutingMode.ADAPTIVE, strategy_set="claude-only")
    assert claude_assessor.id == "claude-fable-high"


def test_builtin_claude_only_routes_simple_to_opus_and_deep_to_fable() -> None:
    strategies = OperatorConfig().execution_strategies(RoutingMode.ADAPTIVE, "claude-only")
    allowed_ids = tuple(strategy.id for strategy in strategies)

    def selected(profile: SemanticTaskProfile) -> str:
        assessment = merge_semantic_profile(
            assess_task(
                "Route the profiled task",
                run_id="run.claude-only",
                required_capabilities=("edit_intent", "process"),
            ),
            profile,
        )
        return select_strategy(
            strategies,
            mode=RoutingMode.ADAPTIVE,
            assessment=assessment,
            allowed_strategy_ids=allowed_ids,
            allowed_backends=("claude_code_cli",),
        ).id

    simple_profile = SemanticTaskProfile(
        task_type=SemanticTaskType.IMPLEMENTATION,
        reasoning_class=SemanticReasoningClass.SIMPLE,
        scope=SemanticScope.BOUNDED,
        ambiguity=SemanticAmbiguity.LOW,
        reasons=("bounded implementation with one obvious inference",),
    )
    deep_profile = SemanticTaskProfile(
        task_type=SemanticTaskType.IMPLEMENTATION,
        reasoning_class=SemanticReasoningClass.DEEP,
        scope=SemanticScope.BOUNDED,
        ambiguity=SemanticAmbiguity.LOW,
        reasons=("bounded implementation requiring deep reasoning",),
    )

    assert selected(simple_profile) == "claude-opus-high"
    assert selected(deep_profile) == "claude-fable-high"


def test_execution_strategy_conversion_preserves_every_configured_field() -> None:
    configured = OperatorStrategyConfig(
        id="strategy.codex",
        backend="codex_cli",
        model="gpt-5",
        effort="high",
        capabilities=("repository_read", "python_edit"),
        planner_eligible=True,
        min_complexity=3,
        max_complexity=8,
        min_scale=2,
        max_scale=7,
        max_risk=4,
    )
    config = OperatorConfig(routing=OperatorRoutingConfig(strategies=(configured,)))

    assert config.execution_strategies(RoutingMode.ADAPTIVE) == (
        ExecutionStrategy(
            id="strategy.codex",
            routing_mode=RoutingMode.ADAPTIVE,
            backend="codex_cli",
            model="gpt-5",
            effort="high",
            capabilities=("repository_read", "python_edit"),
            min_complexity=3,
            max_complexity=8,
            min_scale=2,
            max_scale=7,
            max_risk=4,
        ),
    )
    assert config.planner_strategies(RoutingMode.ADAPTIVE) == config.execution_strategies(
        RoutingMode.ADAPTIVE
    )


def test_planner_candidates_require_explicit_operator_eligibility() -> None:
    planner = OperatorStrategyConfig(
        id="planner",
        backend="codex_cli",
        model="planner-model",
        effort="high",
        planner_eligible=True,
    )
    worker = OperatorStrategyConfig(
        id="worker",
        backend="codex_cli",
        model="worker-model",
        effort="medium",
    )
    config = OperatorConfig(
        routing=OperatorRoutingConfig(
            strategies=(worker, planner),
            strategy_sets={"all": ("worker", "planner")},
        )
    )

    assert tuple(item.id for item in config.planner_strategies(RoutingMode.ADAPTIVE, "all")) == (
        "planner",
    )


def test_task_reviewer_requires_explicit_operator_eligibility_and_default() -> None:
    reviewer = OperatorStrategyConfig(
        id="reviewer",
        backend="codex_cli",
        model="reviewer-model",
        effort="high",
        task_reviewer_eligible=True,
    )
    config = OperatorConfig(
        routing=OperatorRoutingConfig(
            strategies=(reviewer,),
            default_strategy_set="review",
            default_task_reviewer_strategy="reviewer",
            strategy_sets={"review": ("reviewer",)},
        )
    )

    assert config.task_reviewer_strategy(RoutingMode.ADAPTIVE).id == "reviewer"
    with pytest.raises(ValidationError, match="reviewer-eligible"):
        OperatorRoutingConfig(
            strategies=(reviewer.model_copy(update={"task_reviewer_eligible": False}),),
            default_task_reviewer_strategy="reviewer",
        )


def test_disabled_task_review_preserves_pre_issue7_operator_digest() -> None:
    config = OperatorConfig()
    old_payload = config.model_dump(mode="python")
    routing = old_payload["routing"]
    assert isinstance(routing, dict)
    routing.pop("default_task_reviewer_strategy")
    for strategy in routing["strategies"]:
        strategy.pop("task_reviewer_eligible")

    assert operator_config_digest(config) == canonical_digest(old_payload)


def test_named_strategy_set_limits_available_strategies() -> None:
    codex = OperatorStrategyConfig(id="codex", backend="codex_cli", model="gpt-5", effort="medium")
    claude = OperatorStrategyConfig(
        id="claude", backend="claude_code_cli", model="claude-exact", effort="high"
    )
    config = OperatorConfig(
        routing=OperatorRoutingConfig(
            strategies=(codex, claude),
            default_strategy_set="claude-only",
            default_assessment_strategy="codex",
            strategy_sets={"claude-only": ("claude",), "mixed": ("codex", "claude")},
            strategy_set_assessors={"claude-only": "claude"},
        )
    )

    selected = config.execution_strategies(RoutingMode.ADAPTIVE)

    assert tuple(strategy.id for strategy in selected) == ("claude",)
    assert selected[0].backend == "claude_code_cli"
    assert config.strategy_set_name() == "claude-only"
    assert config.strategy_set_name("mixed") == "mixed"
    assert config.assessment_strategy(RoutingMode.ADAPTIVE).id == "claude"
    assert config.assessment_strategy(RoutingMode.ADAPTIVE, strategy_set="mixed").id == "codex"
    assert config.assessment_strategy(RoutingMode.ADAPTIVE, "claude").id == "claude"
    with pytest.raises(ValueError, match="unknown strategy set"):
        config.execution_strategies(RoutingMode.ADAPTIVE, "missing")


def test_routing_rejects_empty_or_duplicate_strategies_and_capabilities() -> None:
    strategy = OperatorStrategyConfig(
        id="strategy.codex",
        backend="codex_cli",
        model="gpt-5",
        effort="medium",
    )

    with pytest.raises(ValidationError, match="effort"):
        OperatorStrategyConfig(id="strategy.missing-effort", backend="codex_cli", model="gpt-5")
    with pytest.raises(ValidationError):
        OperatorRoutingConfig(strategies=())
    with pytest.raises(ValidationError, match="IDs must be unique"):
        OperatorRoutingConfig(strategies=(strategy, strategy))
    with pytest.raises(ValidationError, match="capabilities must be unique"):
        OperatorStrategyConfig(
            id="strategy.duplicate-capabilities",
            backend="codex_cli",
            model="gpt-5",
            effort="high",
            capabilities=("repository_read", "repository_read"),
        )
    with pytest.raises(ValidationError, match="must not be empty"):
        OperatorRoutingConfig(strategies=(strategy,), strategy_sets={"empty": ()})
    with pytest.raises(ValidationError, match="unknown strategy IDs"):
        OperatorRoutingConfig(strategies=(strategy,), strategy_sets={"invalid": ("missing",)})
    with pytest.raises(ValidationError, match="default strategy set"):
        OperatorRoutingConfig(strategies=(strategy,), default_strategy_set="missing")
    with pytest.raises(ValidationError, match="default assessment strategy"):
        OperatorRoutingConfig(strategies=(strategy,), default_assessment_strategy="missing")
    with pytest.raises(ValidationError, match="unknown strategy sets"):
        OperatorRoutingConfig(
            strategies=(strategy,),
            strategy_set_assessors={"missing": "strategy.codex"},
        )
    with pytest.raises(ValidationError, match="unknown strategy IDs"):
        OperatorRoutingConfig(
            strategies=(strategy,),
            strategy_sets={"configured": ("strategy.codex",)},
            strategy_set_assessors={"configured": "missing"},
        )


@pytest.mark.parametrize(
    "bounds",
    (
        {"min_complexity": 0},
        {"max_complexity": 11},
        {"min_complexity": 8, "max_complexity": 7},
        {"min_scale": 8, "max_scale": 7},
        {"max_risk": 11},
    ),
)
def test_strategy_config_rejects_invalid_bounds(bounds: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        OperatorStrategyConfig.model_validate(
            {
                "id": "strategy.invalid-bounds",
                "backend": "codex_cli",
                "model": "gpt-5",
                "effort": "medium",
                **bounds,
            },
            strict=True,
        )
