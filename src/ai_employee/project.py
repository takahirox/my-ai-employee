"""Non-destructive Project Harness discovery and provisional inference."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .domain import ProjectProfile, ProvenanceKind, ProvenancedValue
from .serialization import loads_yaml_model

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


def infer_project_profile(root: str | Path) -> ProjectProfile:
    """Infer common commands without writing a .fleet directory."""

    project_root = Path(root).resolve()
    commands: dict[str, str] = {}
    sources: list[str] = []
    if (project_root / "pyproject.toml").is_file():
        commands.update({"test": "python -m unittest discover -s tests", "build": "python -m build"})
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
            value={"kind": "discovered_file", "path": source},
            provenance=ProvenanceKind.INFERRED, source_reference=source, provisional=True,
        )
        for source in sources
    )
    return ProjectProfile(
        id="inferred-project", commands=commands, rules=rules,
        canonical_document_refs=documents,
    )


def profile_template(profile_id: str = "project") -> dict[str, Any]:
    """Return safe explicit defaults suitable for a human-authored .fleet profile."""

    return {
        "schema_version": "1", "id": profile_id, "profile_version": "1", "root": ".",
        "commands": {}, "rules": [], "protected_paths": [".git/**"], "generated_paths": [],
        "completion_defaults": {}, "contracts": [], "verification_requirements": [],
        "review_rules": [], "canonical_document_refs": ["README.md"],
        "workspace_preferences": {"network": False, "sandbox": "workspace-write"},
    }
