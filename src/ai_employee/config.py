"""Machine-local operator configuration for worker CLI resolution."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from .domain.base import FrozenDict

WorkerName = Literal["codex_cli", "claude_code_cli"]
CONFIG_ENVIRONMENT_VARIABLE = "MY_AI_EMPLOYEE_CONFIG"
DEFAULT_EXECUTABLES: Mapping[WorkerName, str] = {
    "codex_cli": "codex",
    "claude_code_cli": "claude",
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


class OperatorConfig(BaseModel):
    """Host-specific configuration; never grants project execution authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    workers: Mapping[str, WorkerCommandConfig] = Field(default_factory=dict)

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
