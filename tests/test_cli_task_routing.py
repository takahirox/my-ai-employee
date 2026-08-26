from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_employee import cli
from ai_employee.inspector import inspect_work_run
from ai_employee.storage import SQLiteStore


def test_work_cli_defaults_to_adaptive_routing() -> None:
    args = cli.build_parser().parse_args(["work", "route this task"])

    assert args.routing_mode == "adaptive"
    assert args.strategy_set is None


def _write_routing_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".fleet").mkdir()
    (repository / "README.md").write_text("routing fixture\n", encoding="utf-8")
    (repository / ".fleet" / "project.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "worker": {
                    "allowed": ["codex_cli"],
                    "allowed_strategy_ids": ["luna", "sol"],
                    "adaptive_routing": True,
                    "local_backend": False,
                },
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(("git", "-C", str(repository), "init", "-q"), check=True)
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "-c",
            "user.email=fleet@example.invalid",
            "-c",
            "user.name=Fleet Test",
            "commit",
            "-qm",
            "base",
        ),
        check=True,
    )

    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        """#!/bin/sh
if [ "$1" = "--version" ]; then
  printf '%s\n' 'codex test'
  exit 0
fi
if [ "$1" = "--help" ]; then
  printf '%s\n' 'Usage: codex exec'
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)

    operator_config = tmp_path / "operator.json"
    operator_config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workers": {"codex_cli": {"executable": str(fake_codex)}},
                "routing": {
                    "default_strategy_set": "codex-all",
                    "strategy_sets": {
                        "small-only": ["luna"],
                        "codex-all": ["luna", "sol"],
                    },
                    "strategies": [
                        {
                            "id": "luna",
                            "backend": "codex_cli",
                            "model": "gpt-5.6-luna",
                            "effort": "medium",
                            "capabilities": ["edit_intent", "process"],
                            "min_complexity": 1,
                            "max_complexity": 2,
                            "min_scale": 1,
                            "max_scale": 2,
                            "max_risk": 0,
                        },
                        {
                            "id": "sol",
                            "backend": "codex_cli",
                            "model": "gpt-5.6-sol",
                            "effort": "high",
                            "capabilities": ["edit_intent", "process"],
                            "min_complexity": 1,
                            "max_complexity": 10,
                            "min_scale": 1,
                            "max_scale": 10,
                            "max_risk": 10,
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    return repository, operator_config, tmp_path / "fleet.db"


def test_default_adaptive_routing_persists_selected_strategy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, operator_config, db_path = _write_routing_fixture(tmp_path)
    goal = "Inspect the request; implement each change; verify the persisted routing"

    result = cli.main(
        [
            "work",
            goal,
            "--repo",
            str(repository),
            "--operator-config",
            str(operator_config),
            "--db",
            str(db_path),
            "--plan-only",
        ]
    )

    assert result == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["status"] == "planned"
    with SQLiteStore(db_path) as store:
        projection = inspect_work_run(store, emitted["run_id"])

    assessment = projection["routing"]["assessment"]
    selected = projection["routing"]["selected_strategy"]
    assert projection["routing"]["strategy_set"] == "codex-all"
    assert projection["state"] == "planned"
    assert projection["run"]["worker"] == "codex_cli"
    assert selected["backend"] == "codex_cli"
    assert selected["model"] == "gpt-5.6-sol"
    assert selected["effort"] == "high"
    assert assessment["complexity"] > 2
    assert assessment["scale"] > 2


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            ("--routing-mode", "adaptive", "--model", "gpt-5.6-sol"),
            "--routing-mode cannot be combined with --model",
        ),
        (
            ("--routing-mode", "adaptive", "--strategy", "sol"),
            "--routing-mode adaptive rejects --strategy",
        ),
        (
            ("--routing-mode", "adaptive", "--worker", "ollama_cli"),
            "--routing-mode cannot be combined with --worker",
        ),
        (
            ("--routing-mode", "legacy", "--strategy-set", "codex-all"),
            "--strategy-set requires fixed or adaptive routing",
        ),
    ),
)
def test_routing_rejects_conflicting_cli_overrides_before_execution(
    arguments: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        cli.main(["work", "route this task", *arguments])
