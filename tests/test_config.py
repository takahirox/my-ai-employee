from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_employee.config import OperatorConfig, default_operator_config_path, load_operator_config
from ai_employee.domain.harness import HarnessIncidentReporting, ProjectHarnessV2


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


def test_harness_incident_reporting_defaults_off_and_forbids_extras() -> None:
    reporting = HarnessIncidentReporting()
    assert reporting.mode == "off"
    assert reporting.outbox_path.startswith("~/.fleet/")
    assert ProjectHarnessV2().incident_reporting == reporting

    with pytest.raises(ValidationError, match="extra"):
        HarnessIncidentReporting.model_validate({"unknown": True})
    with pytest.raises(ValidationError, match="frozen"):
        reporting.mode = "auto"


@pytest.mark.parametrize(
    "target",
    (
        "https://github.com/owner/repository",
        "owner",
        "/owner/repository",
        "owner/repository/extra",
        "owner name/repository",
        "owner/",
    ),
)
def test_harness_incident_reporting_rejects_non_slug_targets(target: str) -> None:
    with pytest.raises(ValidationError, match="public GitHub"):
        HarnessIncidentReporting(
            mode="approval_required",
            target_repository=target,
            repository_key_env="FLEET_INCIDENT_KEY",
        )


def test_harness_incident_reporting_has_no_literal_secret_fields() -> None:
    fields = HarnessIncidentReporting.model_fields
    assert "repository_key" not in fields
    assert "token" not in fields
    reporting = HarnessIncidentReporting(
        mode="approval_required",
        target_repository="owner/repository",
        repository_key_env="FLEET_INCIDENT_KEY",
    )
    assert reporting.repository_key_env == "FLEET_INCIDENT_KEY"


@pytest.mark.parametrize(
    "changes",
    (
        {"target_repository": "owner/repository"},
        {"repository_key_env": "FLEET_INCIDENT_KEY"},
        {"auto_categories": ("trust_kernel_failure",)},
        {"auto_failures": ("runtime_error",)},
    ),
)
def test_off_harness_incident_reporting_rejects_active_authority(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="active authority"):
        HarnessIncidentReporting(**changes)


def test_enabled_harness_incident_reporting_requires_target_and_key_variable() -> None:
    with pytest.raises(ValidationError, match="requires a target repository"):
        HarnessIncidentReporting(mode="approval_required")
    with pytest.raises(ValidationError, match="requires a target repository"):
        HarnessIncidentReporting(mode="approval_required", target_repository="owner/repository")


@pytest.mark.parametrize(
    "changes",
    (
        {"auto_categories": (), "auto_failures": ("runtime_error",)},
        {"auto_categories": ("trust_kernel_failure",), "auto_failures": ()},
        {"auto_categories": ("*",), "auto_failures": ("runtime_error",)},
        {"auto_categories": ("unknown",), "auto_failures": ("runtime_error",)},
        {"auto_categories": ("trust_kernel_failure",), "auto_failures": ("*",)},
    ),
)
def test_auto_harness_incident_reporting_requires_closed_allowlists(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        HarnessIncidentReporting(
            mode="auto",
            target_repository="owner/repository",
            repository_key_env="FLEET_INCIDENT_KEY",
            **changes,
        )


def test_harness_incident_reporting_caps_provisional_deny_and_round_trip() -> None:
    with pytest.raises(ValidationError):
        HarnessIncidentReporting(daily_limit=21)
    with pytest.raises(ValidationError):
        HarnessIncidentReporting(pending_cap=101)

    reporting = HarnessIncidentReporting(
        mode="auto",
        target_repository="owner/repository",
        repository_key_env="FLEET_INCIDENT_KEY",
        auto_categories=("trust_kernel_failure",),
        auto_failures=("runtime_error",),
    )
    with pytest.raises(ValidationError, match="provisional Harness"):
        ProjectHarnessV2(provisional=True, incident_reporting=reporting)

    harness = ProjectHarnessV2(incident_reporting=reporting)
    assert ProjectHarnessV2.model_validate_json(harness.model_dump_json(), strict=True) == harness
