"""Non-destructive Project Harness discovery and provisional inference."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .domain import ProjectProfile, ProvenancedValue, ProvenanceKind
from .domain.base import freeze_json
from .domain.harness import (
    HarnessEvaluator,
    ProjectHarnessV2,
    project_profile_v1_to_harness,
)
from .serialization import canonical_digest, dumps_yaml, loads_yaml_model

PROFILE_PATHS = (".fleet/project.yaml", ".fleet/project.yml", ".fleet/project.json")
CANONICAL_DOCUMENTS = ("AGENTS.md", "CLAUDE.md", "README.md", "CONTRIBUTING.md")


def discover_project_profile(root: str | Path) -> ProjectProfile:
    """Load explicit profile data or infer a provisional in-memory profile."""

    project_root = Path(root).resolve()
    for relative in PROFILE_PATHS:
        candidate = project_root / relative
        if candidate.is_file():
            profile = loads_yaml_model(candidate.read_text(encoding="utf-8"), ProjectProfile)
            return profile
    return infer_project_profile(project_root)


def discover_project(root: str | Path) -> ProjectProfile | ProjectHarnessV2:
    """Dispatch explicit project configuration without changing v1 wire semantics."""

    project_root = Path(root).resolve()
    for relative in PROFILE_PATHS:
        candidate = project_root / relative
        if candidate.is_file():
            data = _load_mapping(candidate.read_text(encoding="utf-8"))
            version = data.get("schema_version", "1")
            if version == 2:
                return _derive_required_process_evaluators(
                    ProjectHarnessV2.model_validate_json(
                        json.dumps(data, ensure_ascii=False), strict=True
                    )
                )
            if version == "1":
                return ProjectProfile.model_validate_json(
                    json.dumps(data, ensure_ascii=False), strict=True
                )
            raise ValueError(f"unsupported Project Harness schema version: {version!r}")
    return infer_project_profile(project_root)


def discover_project_harness(root: str | Path) -> ProjectHarnessV2:
    """Read v2 or safely convert v1/inferred configuration in memory."""

    discovered = discover_project(root)
    if isinstance(discovered, ProjectHarnessV2):
        return discovered
    return project_profile_v1_to_harness(discovered)


def _derive_required_process_evaluators(harness: ProjectHarnessV2) -> ProjectHarnessV2:
    """Bind legacy required commands to deterministic first-party process evaluators."""

    verification = harness.verification
    if verification.required_evaluators or not verification.required:
        return harness
    existing_ids = {item.id for item in harness.evaluators}
    derived: list[HarnessEvaluator] = []
    for command_ref in verification.required:
        token = canonical_digest(
            {"provider_id": "process.harness", "command_ref": command_ref}
        )
        evaluator_id = f"compat.process.harness.{token}"
        if evaluator_id in existing_ids:
            raise ValueError("derived process.harness evaluator ID collides with a declaration")
        existing_ids.add(evaluator_id)
        derived.append(
            HarnessEvaluator(
                id=evaluator_id,
                provider_id="process.harness",
                command_ref=command_ref,
                criterion_ids=(f"compat.criterion.process.harness.{token}",),
            )
        )
    required_evaluators = tuple(item.id for item in derived)
    return harness.model_copy(
        update={
            "evaluators": (*harness.evaluators, *derived),
            "verification": verification.model_copy(
                update={"required_evaluators": required_evaluators}
            ),
        }
    )


def migration_candidate(root: str | Path) -> str:
    """Render a reviewable v2 candidate without modifying the source profile."""

    return dumps_yaml(discover_project_harness(root))


def write_migration_candidate(root: str | Path, output: str | Path) -> Path:
    """Write a v2 candidate only to the caller-selected output path."""

    project_root = Path(root).resolve()
    destination = Path(output).resolve()
    source_paths = {(project_root / relative).resolve() for relative in PROFILE_PATHS}
    if destination in source_paths:
        raise ValueError("migration output must not overwrite the source project profile")
    if destination.exists():
        raise FileExistsError(f"migration output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(migration_candidate(root), encoding="utf-8")
    return destination


def _load_mapping(text: str) -> dict[str, Any]:
    """Load JSON/YAML while rejecting duplicate mapping keys."""

    try:
        data = json.loads(text, object_pairs_hook=_unique_mapping)
    except json.JSONDecodeError:
        import yaml

        class UniqueLoader(yaml.SafeLoader):  # type: ignore[misc]
            pass

        def construct_mapping(
            loader: UniqueLoader, node: yaml.MappingNode, deep: bool = False
        ) -> dict[str, Any]:
            pairs = loader.construct_pairs(node, deep=deep)
            return _unique_mapping(pairs)

        UniqueLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
        )
        data = yaml.load(text, Loader=UniqueLoader)
    if not isinstance(data, dict):
        raise ValueError("Project Harness document must be a mapping")
    return data


def _unique_mapping(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str):
            raise ValueError("Project Harness mapping keys must be strings")
        if key in result:
            raise ValueError(f"duplicate Project Harness key: {key}")
        result[key] = value
    return result


def infer_project_profile(root: str | Path) -> ProjectProfile:
    """Infer common commands without writing a .fleet directory."""

    project_root = Path(root).resolve()
    commands: dict[str, str] = {}
    sources: list[str] = []
    if (project_root / "pyproject.toml").is_file():
        commands.update(
            {"test": "python -m unittest discover -s tests", "build": "python -m build"}
        )
        sources.append("pyproject.toml")
    if (project_root / "package.json").is_file():
        try:
            package = json.loads((project_root / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            package = {}
        scripts = package.get("scripts", {}) if isinstance(package, Mapping) else {}
        if isinstance(scripts, Mapping):
            for name in ("test", "build", "lint"):
                if name in scripts:
                    commands[name] = f"npm run {name}"
        sources.append("package.json")
    documents = tuple(name for name in CANONICAL_DOCUMENTS if (project_root / name).is_file())
    rules = tuple(
        ProvenancedValue(
            value=freeze_json({"kind": "discovered_file", "path": source}),
            provenance=ProvenanceKind.INFERRED,
            source_reference=source,
            provisional=True,
        )
        for source in sources
    )
    return ProjectProfile(
        id="inferred-project",
        commands=freeze_json(commands),
        rules=rules,
        canonical_document_refs=documents,
    )


def profile_template(profile_id: str = "project") -> dict[str, Any]:
    """Return safe explicit defaults suitable for a human-authored .fleet profile."""

    return {
        "schema_version": "1",
        "id": profile_id,
        "profile_version": "1",
        "root": ".",
        "commands": {},
        "rules": [],
        "protected_paths": [".git/**"],
        "generated_paths": [],
        "completion_defaults": {},
        "contracts": [],
        "verification_requirements": [],
        "review_rules": [],
        "canonical_document_refs": ["README.md"],
        "workspace_preferences": {"network": False, "sandbox": "workspace-write"},
    }
