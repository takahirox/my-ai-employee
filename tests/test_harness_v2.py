from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_employee import cli
from ai_employee.domain import (
    BrowserAction,
    BrowserCapture,
    BrowserScenario,
    ProjectHarnessV2,
    ProvenancedValue,
    ProvenanceKind,
)
from ai_employee.project import (
    discover_project,
    discover_project_harness,
    migration_candidate,
    write_migration_candidate,
)
from ai_employee.serialization import canonical_digest, project_harness_digest


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


def browser_scenario() -> BrowserScenario:
    return BrowserScenario(
        origin="http://127.0.0.1:4173",
        actions=(BrowserAction(kind="navigate", url="http://127.0.0.1:4173/"),),
        captures=(
            BrowserCapture(
                id="screen",
                kind="screenshot",
                logical_kind="browser_screenshot",
            ),
        ),
    )


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


def test_parent_semantic_review_requires_explicit_harness_review_gate() -> None:
    enabled = valid_harness()
    enabled["verification"]["review"]["parent_semantic_review"] = True
    harness = ProjectHarnessV2.model_validate_json(json.dumps(enabled), strict=True)
    assert harness.verification.review.parent_semantic_review

    enabled["verification"]["review"]["required"] = False
    with pytest.raises(ValidationError, match="requires the review gate"):
        ProjectHarnessV2.model_validate_json(json.dumps(enabled), strict=True)


def test_parent_semantic_review_rejects_empty_blocking_policy() -> None:
    enabled = valid_harness()
    enabled["verification"]["review"]["parent_semantic_review"] = True
    enabled["verification"]["review"]["block_severities"] = []

    with pytest.raises(ValidationError, match="requires blocking severities"):
        ProjectHarnessV2.model_validate_json(json.dumps(enabled), strict=True)


def test_disabled_task_review_preserves_pre_issue7_harness_digest() -> None:
    harness = ProjectHarnessV2.model_validate_json(json.dumps(valid_harness()), strict=True)
    old_payload = harness.model_dump(mode="python")
    old_payload["verification"]["review"].pop("independent_task_review")
    old_payload["verification"]["review"].pop("parent_semantic_review")

    assert project_harness_digest(harness) == canonical_digest(old_payload)

    enabled = harness.model_copy(
        update={
            "verification": harness.verification.model_copy(
                update={
                    "review": harness.verification.review.model_copy(
                        update={"independent_task_review": True}
                    )
                }
            )
        }
    )
    assert project_harness_digest(enabled) != canonical_digest(old_payload)

    parent_enabled = harness.model_copy(
        update={
            "verification": harness.verification.model_copy(
                update={
                    "review": harness.verification.review.model_copy(
                        update={"parent_semantic_review": True}
                    )
                }
            )
        }
    )
    assert project_harness_digest(parent_enabled) != canonical_digest(old_payload)


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

    data["evaluators"].append(
        {
            "id": "browser",
            "provider_id": "browser.playwright",
            "browser_scenario": browser_scenario().model_dump(mode="json"),
            "criterion_ids": ["browser-safe"],
        }
    )
    harness = ProjectHarnessV2.model_validate_json(json.dumps(data), strict=True)
    assert harness.evaluators[1].provider_id == "browser.playwright"


def test_browser_evaluator_example_is_a_loadable_explicit_harness() -> None:
    example = Path(__file__).parents[1] / "examples" / "browser-evaluator"

    harness = discover_project_harness(example)

    assert not harness.provisional
    assert harness.verification.required == ("fixture-exists",)
    assert harness.verification.required_evaluators == (
        "fixture-check",
        "interaction-check",
    )
    assert tuple(item.provider_id for item in harness.evaluators) == (
        "process.harness",
        "browser.playwright",
    )
    scenario = harness.evaluators[1].browser_scenario
    assert scenario is not None
    assert tuple(item.kind for item in scenario.actions) == ("navigate", "click")
    assert tuple(item.logical_kind for item in scenario.captures) == (
        "browser_screenshot",
        "browser_console",
        "browser_dom",
        "browser_accessibility",
    )


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
                    "id": "visual",
                    "provider_id": "judge.visual",
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


def test_required_browser_evaluator_defines_scenario_bound_goal_criteria() -> None:
    data = valid_harness()
    scenario = browser_scenario()
    data["evaluators"] = [
        {
            "id": "browser-smoke",
            "provider_id": "browser.playwright",
            "browser_scenario": scenario.model_dump(mode="json"),
            "criterion_ids": ["rendered-result-observed"],
        }
    ]
    data["verification"]["required_evaluators"] = ["browser-smoke"]
    harness = ProjectHarnessV2.model_validate_json(json.dumps(data), strict=True)

    goal = cli._work_goal("browser-run", "observe", harness)

    criterion = goal.completion_criteria[0]
    assert criterion.id == "rendered-result-observed"
    assert criterion.verification_requirement_ids == ()
    assert criterion.required_artifact_ids == ("workspace_patch",)
    assert "browser scenario" in criterion.description


@pytest.mark.parametrize(
    "evaluator",
    [
        {
            "id": "process-with-browser",
            "provider_id": "process.harness",
            "command_ref": "test",
            "browser_scenario": browser_scenario().model_dump(mode="json"),
            "criterion_ids": ["invalid"],
        },
        {
            "id": "browser-with-command",
            "provider_id": "browser.playwright",
            "command_ref": "test",
            "browser_scenario": browser_scenario().model_dump(mode="json"),
            "criterion_ids": ["invalid"],
        },
        {
            "id": "browser-without-scenario",
            "provider_id": "browser.playwright",
            "criterion_ids": ["invalid"],
        },
    ],
)
def test_harness_rejects_mixed_evaluator_authority(evaluator: dict[str, object]) -> None:
    data = valid_harness()
    data["evaluators"] = [evaluator]
    with pytest.raises(ValidationError):
        ProjectHarnessV2.model_validate_json(json.dumps(data), strict=True)


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
