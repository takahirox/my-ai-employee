from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_employee.domain import ProjectHarnessV2, ProvenancedValue, ProvenanceKind
from ai_employee.project import (
    discover_project,
    discover_project_harness,
    migration_candidate,
    write_migration_candidate,
)


def valid_harness() -> dict[str, object]:
    return {
        "schema_version": 2,
        "commands": {"test": {"argv": ["python", "-m", "pytest"], "cwd": "."}},
        "paths": {"writable": ["src/**"], "protected": [".git/**"]},
        "verification": {
            "required": ["test"],
            "review": {"required": True, "block_severities": ["critical", "high"]},
        },
        "network": {"mode": "restricted", "https_domains": ["example.com"], "ports": [443]},
        "install": {
            "ecosystems": ["python_venv"],
            "existing_lock": "allow",
            "new_dependency": "approval",
            "lifecycle_scripts": "deny",
        },
        "approvals": {
            "process_shell": "required",
            "new_dependency": "required",
            "promotion": "required",
        },
        "worker": {"preferred": "codex_cli", "allowed": ["codex_cli"]},
        "budgets": {
            "wall_seconds": 1800.0,
            "worker_turns": 12,
            "processes": 40,
            "download_bytes": 1000,
            "artifact_bytes": 2000,
        },
    }


def test_explicit_harness_is_typed_strict_and_deeply_immutable(tmp_path: Path) -> None:
    profile = tmp_path / ".fleet" / "project.json"
    profile.parent.mkdir()
    profile.write_text(json.dumps(valid_harness()), encoding="utf-8")
    harness = discover_project(tmp_path)
    assert isinstance(harness, ProjectHarnessV2)
    assert harness.verification.required == ("test",)
    with pytest.raises(TypeError):
        harness.commands["lint"] = harness.commands["test"]  # type: ignore[index]
    broken = valid_harness()
    broken["unknown"] = True
    with pytest.raises(ValidationError):
        ProjectHarnessV2.model_validate_json(json.dumps(broken), strict=True)


def test_harness_allows_repeated_argv_but_rejects_overlapping_protected_paths() -> None:
    data = valid_harness()
    data["commands"]["test"]["argv"] = ["python", "-c", "print('x')", "-c"]
    ProjectHarnessV2.model_validate_json(json.dumps(data), strict=True)
    data["paths"] = {"writable": ["src/**"], "protected": ["src/secrets/**"]}
    with pytest.raises(ValidationError, match="writable and protected"):
        ProjectHarnessV2.model_validate_json(json.dumps(data), strict=True)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["verification"].update(required=["missing"]),
        lambda data: data["paths"].update(writable=[".git/**"]),
        lambda data: data["commands"]["test"].update(cwd="../escape"),
        lambda data: data["worker"].update(preferred="claude_code_cli"),
    ],
)
def test_harness_rejects_contradictions_and_escaping_paths(mutation: object) -> None:
    data = valid_harness()
    mutation(data)  # type: ignore[operator]
    with pytest.raises(ValidationError):
        ProjectHarnessV2.model_validate_json(json.dumps(data), strict=True)


def test_duplicate_yaml_keys_and_unknown_versions_fail_closed(tmp_path: Path) -> None:
    profile = tmp_path / ".fleet" / "project.yaml"
    profile.parent.mkdir()
    profile.write_text("schema_version: 2\ncommands: {}\ncommands: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        discover_project(tmp_path)
    profile.write_text("schema_version: 3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported"):
        discover_project(tmp_path)


def test_v1_conversion_is_provisional_restrictive_and_non_destructive(tmp_path: Path) -> None:
    profile = tmp_path / ".fleet" / "project.json"
    profile.parent.mkdir()
    source = {
        "schema_version": "1",
        "id": "legacy",
        "profile_version": "1",
        "root": ".",
        "commands": {"test": "python -m pytest"},
        "rules": [
            {
                "schema_version": "1",
                "value": {"kind": "review-guidance"},
                "provenance": "explicit",
                "source_reference": "AGENTS.md",
                "provisional": False,
            }
        ],
        "protected_paths": [".git/**"],
        "generated_paths": ["dist/**"],
        "completion_defaults": None,
        "contracts": [],
        "verification_requirements": [],
        "review_rules": [],
        "canonical_document_refs": [],
        "workspace_preferences": None,
    }
    original = json.dumps(source)
    profile.write_text(original, encoding="utf-8")
    harness = discover_project_harness(tmp_path)
    assert harness.provisional
    assert harness.migrated_from == "1"
    assert harness.commands["test"].argv == ("python", "-m", "pytest")
    assert harness.rules == (
        ProvenancedValue(
            value={"kind": "review-guidance"},
            provenance=ProvenanceKind.EXPLICIT,
            source_reference="AGENTS.md",
        ),
    )
    assert harness.paths.generated == ("dist/**",)
    assert harness.network.mode.value == "disabled"
    assert harness.install.ecosystems == ()
    assert harness.worker.allowed == ()
    rendered = migration_candidate(tmp_path)
    assert '"schema_version": 2' in rendered
    assert profile.read_text(encoding="utf-8") == original


def test_migration_output_cannot_overwrite_source_or_existing_file(tmp_path: Path) -> None:
    profile = tmp_path / ".fleet" / "project.json"
    profile.parent.mkdir()
    profile.write_text(json.dumps(valid_harness()), encoding="utf-8")
    with pytest.raises(ValueError, match="must not overwrite"):
        write_migration_candidate(tmp_path, profile)
    destination = tmp_path / "candidate.json"
    destination.write_text("preserve", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_migration_candidate(tmp_path, destination)
    assert destination.read_text(encoding="utf-8") == "preserve"
