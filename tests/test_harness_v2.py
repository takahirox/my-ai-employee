from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_employee import cli
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
    assert not harness.verification.review.independent_task_review
    with pytest.raises(TypeError):
        harness.commands["lint"] = harness.commands["test"]  # type: ignore[index]
    broken = valid_harness()
    broken["unknown"] = True
    with pytest.raises(ValidationError):
        ProjectHarnessV2.model_validate_json(json.dumps(broken), strict=True)


def test_independent_task_review_requires_explicit_harness_review_gate() -> None:
    enabled = valid_harness()
    enabled["verification"]["review"]["independent_task_review"] = True
    harness = ProjectHarnessV2.model_validate_json(json.dumps(enabled), strict=True)
    assert harness.verification.review.independent_task_review

    enabled["verification"]["review"]["required"] = False
    with pytest.raises(ValidationError, match="requires the review gate"):
        ProjectHarnessV2.model_validate_json(json.dumps(enabled), strict=True)


def test_harness_allows_repeated_argv_but_rejects_overlapping_protected_paths() -> None:
    data = valid_harness()
    data["commands"]["test"]["argv"] = ["python", "-c", "print('x')", "-c"]
    ProjectHarnessV2.model_validate_json(json.dumps(data), strict=True)
    data["paths"] = {"writable": ["src/**"], "protected": ["src/secrets/**"]}
    with pytest.raises(ValidationError, match="writable and protected"):
        ProjectHarnessV2.model_validate_json(json.dumps(data), strict=True)


def test_harness_accepts_strict_process_evaluator_declarations() -> None:
    data = valid_harness()
    data["evaluators"] = [
        {
            "id": "unit-tests",
            "provider_id": "process.harness",
            "command_ref": "test",
            "criterion_ids": ["tests-pass"],
        }
    ]
    data["verification"]["required_evaluators"] = ["unit-tests"]
    harness = ProjectHarnessV2.model_validate_json(json.dumps(data), strict=True)
    assert harness.evaluators[0].command_ref == "test"
    assert harness.verification.required_evaluators == ("unit-tests",)


@pytest.mark.parametrize(
    ("evaluators", "required", "message"),
    [
        (
            [
                {
                    "id": "duplicate",
                    "provider_id": "process.harness",
                    "command_ref": "test",
                    "criterion_ids": ["one"],
                },
                {
                    "id": "duplicate",
                    "provider_id": "process.harness",
                    "command_ref": "test",
                    "criterion_ids": ["two"],
                },
            ],
            [],
            "unique",
        ),
        (
            [{"id": "x", "provider_id": "unknown", "criterion_ids": ["one"]}],
            [],
            "unknown evaluator provider",
        ),
        (
            [
                {
                    "id": "browser",
                    "provider_id": "browser.playwright",
                    "criterion_ids": ["visual"],
                }
            ],
            [],
            "reserved but unavailable",
        ),
        ([], ["missing"], "unknown evaluators"),
    ],
)
def test_harness_rejects_inconsistent_evaluator_declarations(
    evaluators: list[dict[str, object]], required: list[str], message: str
) -> None:
    data = valid_harness()
    data["evaluators"] = evaluators
    data["verification"]["required_evaluators"] = required
    with pytest.raises(ValidationError, match=message):
        ProjectHarnessV2.model_validate_json(json.dumps(data), strict=True)


def test_required_evaluator_defines_parent_goal_criteria() -> None:
    data = valid_harness()
    data["evaluators"] = [
        {
            "id": "unit-tests",
            "provider_id": "process.harness",
            "command_ref": "test",
            "criterion_ids": ["tests-pass"],
        }
    ]
    data["verification"]["required_evaluators"] = ["unit-tests"]
    harness = ProjectHarnessV2.model_validate_json(json.dumps(data), strict=True)
    goal = cli._work_goal("run-evaluator", "evaluate", harness)

    assert tuple(item.id for item in goal.completion_criteria) == ("tests-pass",)
    assert goal.completion_criteria[0].verification_requirement_ids == ("test",)
    assert goal.completion_criteria[0].required_artifact_ids == ("workspace_patch",)


def test_discovery_derives_legacy_required_process_evaluators_once(tmp_path: Path) -> None:
    profile = tmp_path / ".fleet" / "project.json"
    profile.parent.mkdir()
    profile.write_text(json.dumps(valid_harness()), encoding="utf-8")

    first = discover_project_harness(tmp_path)
    second = discover_project_harness(tmp_path)

    assert first == second
    assert len(first.evaluators) == 1
    evaluator = first.evaluators[0]
    assert evaluator.provider_id == "process.harness"
    assert evaluator.command_ref == "test"
    assert first.verification.required_evaluators == (evaluator.id,)
    goal = cli._work_goal("compat-run", "verify compatibility", first)
    assert tuple(item.id for item in goal.completion_criteria) == evaluator.criterion_ids
    assert goal.completion_criteria[0].verification_requirement_ids == ("test",)


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
