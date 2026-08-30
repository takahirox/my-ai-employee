from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_employee.config import OperatorConfig, default_operator_config_path, load_operator_config


def test_optional_default_config_and_explicit_worker_override(tmp_path: Path) -> None:
    environment = {"HOME": str(tmp_path)}
    assert load_operator_config(environment=environment) == OperatorConfig()
    config_path = default_operator_config_path(environment)
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """schema_version: 1
workers:
  codex_cli:
    executable: /custom/bin/codex
    path_entries: [/runtime/bin]
""",
        encoding="utf-8",
    )
    config = load_operator_config(environment=environment)
    assert config.worker_command("codex_cli").executable == "/custom/bin/codex"
    assert config.worker_command("codex_cli").path_entries == ("/runtime/bin",)
    assert config.worker_command("claude_code_cli").executable == "claude"
    assert config.worker_command("ollama_cli").executable == "ollama"


def test_explicit_missing_config_duplicate_keys_and_relative_paths_fail_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        load_operator_config(tmp_path / "missing.yaml")
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text("schema_version: 1\nworkers: {}\nworkers: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_operator_config(duplicate)
    relative = tmp_path / "relative.yaml"
    relative.write_text(
        "schema_version: 1\nworkers:\n  codex_cli:\n    executable: ../codex\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="absolute"):
        load_operator_config(relative)


def test_promotion_auto_approval_rejects_always_and_relative_repositories() -> None:
    with pytest.raises(ValidationError):
        OperatorConfig.model_validate_json(
            '{"promotion_auto_approval":{"mode":"always"}}', strict=True
        )
    with pytest.raises(ValidationError, match="absolute"):
        OperatorConfig.model_validate_json(
            '{"promotion_auto_approval":'
            '{"mode":"policy","allowed_repositories":["relative/repo"]}}',
            strict=True,
        )
