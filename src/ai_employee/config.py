"""Machine-local operator configuration for worker CLI resolution."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from .domain import ExecutionStrategy, RoutingMode
from .domain.base import FrozenDict, Identifier

WorkerName = Literal["codex_cli", "claude_code_cli", "ollama_cli"]
CONFIG_ENVIRONMENT_VARIABLE = "MY_AI_EMPLOYEE_CONFIG"
DEFAULT_EXECUTABLES: Mapping[WorkerName, str] = {
    "codex_cli": "codex",
    "claude_code_cli": "claude",
    "ollama_cli": "ollama",
}


class WorkerCommandConfig(BaseModel):
    """An operator-authorized executable and its runtime dependency paths."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    executable: str = Field(min_length=1, max_length=4_096)
    path_entries: tuple[str, ...] = ()

    @field_validator("executable")
    @classmethod
    def _safe_executable(cls, value: str) -> str:
        if "\x00" in value or "\\" in value:
            raise ValueError("worker executable must be NUL-free and use native path syntax")
        if "/" in value and not Path(value).is_absolute():
            raise ValueError("configured worker executable paths must be absolute")
        return value

    @field_validator("path_entries")
    @classmethod
    def _absolute_unique_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("worker path entries must be unique")
        if any("\x00" in item or not Path(item).is_absolute() for item in value):
            raise ValueError("worker path entries must be absolute and NUL-free")
        return value


class OperatorStrategyConfig(BaseModel):
    """An operator-configured execution strategy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: Identifier
    backend: WorkerName
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(min_length=1, max_length=100)
    capabilities: tuple[Identifier, ...] = Field(default=(), max_length=100)
    min_complexity: int = Field(default=1, ge=1, le=10)
    max_complexity: int = Field(default=10, ge=1, le=10)
    min_scale: int = Field(default=1, ge=1, le=10)
    max_scale: int = Field(default=10, ge=1, le=10)
    max_risk: int = Field(default=10, ge=0, le=10)

    @model_validator(mode="after")
    def _valid_bounds_and_capabilities(self) -> Self:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("strategy capabilities must be unique")
        if self.min_complexity > self.max_complexity:
            raise ValueError("minimum complexity cannot exceed maximum complexity")
        if self.min_scale > self.max_scale:
            raise ValueError("minimum scale cannot exceed maximum scale")
        return self


class OperatorRoutingConfig(BaseModel):
    """The exact execution strategies available to a routing caller."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    strategies: tuple[OperatorStrategyConfig, ...] = Field(min_length=1)
    default_strategy_set: Identifier | None = None
    default_assessment_strategy: Identifier | None = None
    strategy_sets: Mapping[Identifier, tuple[Identifier, ...]] = Field(
        default_factory=dict
    )
    strategy_set_assessors: Mapping[Identifier, Identifier] = Field(
        default_factory=dict
    )

    @field_validator("strategy_sets")
    @classmethod
    def _freeze_strategy_sets(
        cls, value: Mapping[Identifier, tuple[Identifier, ...]]
    ) -> Mapping[Identifier, tuple[Identifier, ...]]:
        for name, strategy_ids in value.items():
            if not strategy_ids:
                raise ValueError(f"strategy set {name!r} must not be empty")
            if len(strategy_ids) != len(set(strategy_ids)):
                raise ValueError(f"strategy set {name!r} must contain unique IDs")
        return FrozenDict(value)

    @field_serializer("strategy_sets")
    def _serialize_strategy_sets(
        self, value: Mapping[Identifier, tuple[Identifier, ...]]
    ) -> dict[Identifier, tuple[Identifier, ...]]:
        return dict(value)

    @field_validator("strategy_set_assessors")
    @classmethod
    def _freeze_strategy_set_assessors(
        cls, value: Mapping[Identifier, Identifier]
    ) -> Mapping[Identifier, Identifier]:
        return FrozenDict(value)

    @field_serializer("strategy_set_assessors")
    def _serialize_strategy_set_assessors(
        self, value: Mapping[Identifier, Identifier]
    ) -> dict[Identifier, Identifier]:
        return dict(value)

    @model_validator(mode="after")
    def _unique_strategy_ids(self) -> Self:
        ids = tuple(strategy.id for strategy in self.strategies)
        if len(ids) != len(set(ids)):
            raise ValueError("operator strategy IDs must be unique")
        unknown = {
            strategy_id
            for strategy_ids in self.strategy_sets.values()
            for strategy_id in strategy_ids
            if strategy_id not in ids
        }
        if unknown:
            raise ValueError(
                f"strategy sets reference unknown strategy IDs: {sorted(unknown)}"
            )
        if (
            self.default_strategy_set is not None
            and self.default_strategy_set not in self.strategy_sets
        ):
            raise ValueError("default strategy set must name a configured strategy set")
        if (
            self.default_assessment_strategy is not None
            and self.default_assessment_strategy not in ids
        ):
            raise ValueError(
                "default assessment strategy must name a configured strategy"
            )
        unknown_sets = set(self.strategy_set_assessors) - set(self.strategy_sets)
        if unknown_sets:
            raise ValueError(
                f"assessment strategies reference unknown strategy sets: {sorted(unknown_sets)}"
            )
        unknown_assessors = set(self.strategy_set_assessors.values()) - set(ids)
        if unknown_assessors:
            raise ValueError(
                "strategy-set assessment strategies reference unknown strategy IDs: "
                f"{sorted(unknown_assessors)}"
            )
        return self


def default_operator_routing_config() -> OperatorRoutingConfig:
    """Built-in cloud-only routing used when the operator does not override it."""

    return OperatorRoutingConfig(
        default_strategy_set="codex-balanced",
        default_assessment_strategy="codex-sol-high",
        strategy_sets={
            "codex-balanced": ("codex-luna-max", "codex-sol-high"),
            "claude-only": ("claude-opus-high", "claude-fable-high"),
        },
        strategy_set_assessors={
            "codex-balanced": "codex-sol-high",
            "claude-only": "claude-fable-high",
        },
        strategies=(
            OperatorStrategyConfig(
                id="codex-luna-max",
                backend="codex_cli",
                model="gpt-5.6-luna",
                effort="max",
                capabilities=("edit_intent", "process"),
                min_complexity=1,
                max_complexity=3,
                min_scale=1,
                max_scale=3,
                max_risk=0,
            ),
            OperatorStrategyConfig(
                id="codex-sol-high",
                backend="codex_cli",
                model="gpt-5.6-sol",
                effort="high",
                capabilities=("edit_intent", "process"),
                min_complexity=1,
                max_complexity=10,
                min_scale=1,
                max_scale=10,
                max_risk=10,
            ),
            OperatorStrategyConfig(
                id="claude-opus-high",
                backend="claude_code_cli",
                model="claude-opus-5",
                effort="high",
                capabilities=("edit_intent", "process"),
                min_complexity=1,
                max_complexity=3,
                min_scale=1,
                max_scale=3,
                max_risk=0,
            ),
            OperatorStrategyConfig(
                id="claude-fable-high",
                backend="claude_code_cli",
                model="claude-fable-5",
                effort="high",
                capabilities=("edit_intent", "process"),
                min_complexity=1,
                max_complexity=10,
                min_scale=1,
                max_scale=10,
                max_risk=10,
            ),
        ),
    )


class OperatorConfig(BaseModel):
    """Host-specific configuration; never grants project execution authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    workers: Mapping[str, WorkerCommandConfig] = Field(default_factory=dict)
    routing: OperatorRoutingConfig | None = Field(
        default_factory=default_operator_routing_config
    )

    @field_validator("workers")
    @classmethod
    def _freeze_workers(
        cls, value: Mapping[str, WorkerCommandConfig]
    ) -> Mapping[str, WorkerCommandConfig]:
        unknown = set(value) - set(DEFAULT_EXECUTABLES)
        if unknown:
            raise ValueError(f"unsupported worker configuration: {sorted(unknown)}")
        return FrozenDict(value)

    @field_serializer("workers")
    def _serialize_workers(
        self, value: Mapping[str, WorkerCommandConfig]
    ) -> dict[str, WorkerCommandConfig]:
        return dict(value)

    def worker_command(self, worker: WorkerName) -> WorkerCommandConfig:
        configured = self.workers.get(worker)
        return configured or WorkerCommandConfig(executable=DEFAULT_EXECUTABLES[worker])

    def execution_strategies(
        self, mode: RoutingMode, strategy_set: str | None = None
    ) -> tuple[ExecutionStrategy, ...]:
        if self.routing is None:
            return ()
        configured = self.routing.strategies
        selected_set = self.strategy_set_name(strategy_set)
        if selected_set is not None:
            selected_ids = self.routing.strategy_sets.get(selected_set)
            if selected_ids is None:
                raise ValueError(f"unknown strategy set: {selected_set}")
            selected = set(selected_ids)
            configured = tuple(
                strategy for strategy in configured if strategy.id in selected
            )
        return tuple(
            ExecutionStrategy(
                id=strategy.id,
                routing_mode=mode,
                backend=strategy.backend,
                model=strategy.model,
                effort=strategy.effort,
                capabilities=strategy.capabilities,
                min_complexity=strategy.min_complexity,
                max_complexity=strategy.max_complexity,
                min_scale=strategy.min_scale,
                max_scale=strategy.max_scale,
                max_risk=strategy.max_risk,
            )
            for strategy in configured
        )

    def strategy_set_name(self, requested: str | None = None) -> str | None:
        if requested is not None:
            return requested
        if self.routing is None:
            return None
        return self.routing.default_strategy_set

    def assessment_strategy(
        self,
        mode: RoutingMode,
        requested: str | None = None,
        strategy_set: str | None = None,
    ) -> ExecutionStrategy:
        if self.routing is None:
            raise ValueError("adaptive routing requires operator routing configuration")
        selected_set = self.strategy_set_name(strategy_set)
        selected_id = (
            requested
            or (
                None
                if selected_set is None
                else self.routing.strategy_set_assessors.get(selected_set)
            )
            or self.routing.default_assessment_strategy
        )
        if selected_id is None:
            raise ValueError("adaptive routing requires an assessment strategy")
        configured = next(
            (strategy for strategy in self.routing.strategies if strategy.id == selected_id),
            None,
        )
        if configured is None:
            raise ValueError(f"unknown assessment strategy: {selected_id}")
        return ExecutionStrategy(
            id=configured.id,
            routing_mode=mode,
            backend=configured.backend,
            model=configured.model,
            effort=configured.effort,
            capabilities=configured.capabilities,
            min_complexity=configured.min_complexity,
            max_complexity=configured.max_complexity,
            min_scale=configured.min_scale,
            max_scale=configured.max_scale,
            max_risk=configured.max_risk,
        )


def default_operator_config_path(
    environment: Mapping[str, str] | None = None,
) -> Path:
    values = os.environ if environment is None else environment
    if explicit := values.get(CONFIG_ENVIRONMENT_VARIABLE):
        return Path(explicit).expanduser()
    if xdg_root := values.get("XDG_CONFIG_HOME"):
        return Path(xdg_root).expanduser() / "my-ai-employee" / "config.yaml"
    home = values.get("HOME")
    base = Path(home).expanduser() if home else Path.home()
    return base / ".config" / "my-ai-employee" / "config.yaml"


def load_operator_config(
    path: str | Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> OperatorConfig:
    """Load an explicit config or the conventional optional machine-local config."""

    values = os.environ if environment is None else environment
    explicit = path is not None or bool(values.get(CONFIG_ENVIRONMENT_VARIABLE))
    candidate = (
        Path(path).expanduser() if path is not None else default_operator_config_path(values)
    )
    if not candidate.is_file():
        if explicit:
            raise FileNotFoundError(f"operator config does not exist: {candidate}")
        return OperatorConfig()
    data = _load_unique_mapping(candidate.read_text(encoding="utf-8"))
    return OperatorConfig.model_validate_json(
        json.dumps(data, ensure_ascii=False, allow_nan=False), strict=True
    )


def _load_unique_mapping(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text, object_pairs_hook=_unique_mapping)
    except json.JSONDecodeError:
        import yaml

        class UniqueLoader(yaml.SafeLoader):  # type: ignore[misc]
            pass

        def construct_mapping(
            loader: UniqueLoader, node: yaml.MappingNode, deep: bool = False
        ) -> dict[str, Any]:
            return _unique_mapping(loader.construct_pairs(node, deep=deep))

        UniqueLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
        )
        data = yaml.load(text, Loader=UniqueLoader)
    if not isinstance(data, dict):
        raise ValueError("operator config document must be a mapping")
    return data


def _unique_mapping(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str):
            raise ValueError("operator config mapping keys must be strings")
        if key in result:
            raise ValueError(f"duplicate operator config key: {key}")
        result[key] = value
    return result
