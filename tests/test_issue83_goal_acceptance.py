from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai_employee import cli
from ai_employee.domain import Goal
from ai_employee.domain.v2 import ApprovalRecord
from ai_employee.goal_acceptance import PREFIX, GoalCheck, attach_goal_checks, harness_for_goal
from ai_employee.project import discover_project_harness
from ai_employee.serialization import project_harness_digest
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import GraphRunRecord
from tests.test_cli_graph_e2e import _fixture, _write_executable

GOAL = "change c.txt"


def test_model_exposes_runtime_prefix_length_requirement():
    assert GoalCheck.model_json_schema()["properties"]["id"]["maxLength"] == 128 - len(PREFIX)
    with pytest.raises(ValueError):
        GoalCheck(id="a" * 128, request_fragment="x", description="x", command_ref="test")


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.delenv("FLEET_DB", raising=False)
    monkeypatch.setattr(
        cli, "resolve_database_path", lambda *_args, **_kwargs: tmp_path / "fleet.db"
    )


def fixture(tmp_path: Path, *, correct: bool):
    repository, operator, database, state = _fixture(tmp_path, task_review=False)
    config_path = repository / ".fleet/project.json"
    config = json.loads(config_path.read_text())
    config["commands"]["goal-check"] = {
        "argv": [
            sys.executable,
            "-I",
            "-c",
            "from pathlib import Path; assert Path('c.txt').read_text() == 'c-after\\n'",
        ]
    }
    config_path.write_text(json.dumps(config))
    _write_executable(tmp_path / "fake-parent-verifier", "print('shared checks pass')")
    for name in ("a", "b"):
        (state / f"{name}.done").write_text("done")
    if not correct:
        worker = tmp_path / "fake-worker"
        worker.write_text(worker.read_text().replace('f"+{name}-after', 'f"+{name}-wrong'))
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "acceptance fixture",
        ],
        check=True,
    )
    acceptance = tmp_path / "goal-checks.json"
    acceptance.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "goal": GOAL,
                "criteria": [
                    {
                        "id": "requested-content",
                        "request_fragment": "c.txt",
                        "description": "c.txt contains exactly c-after followed by a newline",
                        "command_ref": "goal-check",
                    }
                ],
            }
        )
    )
    return repository, operator, database, acceptance


@pytest.mark.parametrize(
    "enabled,correct,success", [(False, False, True), (True, False, False), (True, True, True)]
)
def test_cli_request_specific_check_catches_defect_shared_checks_miss(
    tmp_path: Path,
    capsys,
    enabled: bool,
    correct: bool,
    success: bool,
) -> None:
    repository, operator, database, acceptance = fixture(tmp_path, correct=correct)
    args = [
        "work",
        GOAL,
        "--repo",
        str(repository),
        "--operator-config",
        str(operator),
        "--routing-mode",
        "fixed",
        "--strategy",
        "planner",
        "--non-interactive",
    ]
    if enabled:
        args += ["--acceptance-file", str(acceptance)]
    cli.main(args)
    emitted = json.loads(capsys.readouterr().out)
    assert (emitted["status"] == "ready_to_promote") == success, emitted
    assert (repository / "c.txt").read_text() == "c-before\n"  # no automatic promotion
    with SQLiteStore(database) as store:
        run = store.get("graph_run_v2", emitted["run_id"], GraphRunRecord)
        criteria = [c for c in run.goal.completion_criteria if c.id.startswith("goal.acceptance.")]
        assert len(criteria) == int(enabled)
        if enabled:
            assert "[request: c.txt]" in criteria[0].description
            harness = harness_for_goal(discover_project_harness(repository), run.goal)
            assert project_harness_digest(harness) == run.harness_digest
            # Deleting or editing the input file cannot relax the persisted goal.
            acceptance.write_text("{}")
            assert harness_for_goal(discover_project_harness(repository), run.goal) == harness


def test_unknown_command_foreign_goal_and_weakened_criterion_are_rejected(tmp_path: Path) -> None:
    repository, _, _, acceptance = fixture(tmp_path, correct=True)
    harness = discover_project_harness(repository)
    with pytest.raises(ValueError, match="exact mutating Goal"):
        attach_goal_checks(Goal(id="g", statement="different request"), acceptance)
    goal = attach_goal_checks(Goal(id="g", statement=GOAL), acceptance)
    bad = goal.model_copy(
        update={
            "completion_criteria": (
                goal.completion_criteria[0].model_copy(update={"mandatory": False}),
            )
        }
    )
    with pytest.raises(ValueError, match="weakened"):
        harness_for_goal(harness, bad)
    bad = goal.model_copy(
        update={
            "completion_criteria": (
                goal.completion_criteria[0].model_copy(
                    update={"verification_requirement_ids": ("unknown",)}
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="already be declared"):
        harness_for_goal(harness, bad)


def test_candidate_writable_test_script_is_not_accepted_as_frozen_check(tmp_path: Path) -> None:
    repository, _, _, acceptance = fixture(tmp_path, correct=True)
    config_path = repository / ".fleet/project.json"
    config = json.loads(config_path.read_text())
    config["commands"]["goal-check"]["argv"] = [sys.executable, "tests/agent_can_edit.py"]
    config_path.write_text(json.dumps(config))
    goal = attach_goal_checks(Goal(id="g", statement=GOAL), acceptance)
    with pytest.raises(ValueError, match="python -I -c"):
        harness_for_goal(discover_project_harness(repository), goal)


@pytest.mark.parametrize("weaken_harness", [False, True])
def test_promotion_replays_frozen_goal_and_rejects_weakened_check(
    tmp_path: Path,
    capsys,
    weaken_harness: bool,
) -> None:
    repository, operator, database, acceptance = fixture(tmp_path, correct=True)
    assert (
        cli.main(
            [
                "work",
                GOAL,
                "--repo",
                str(repository),
                "--operator-config",
                str(operator),
                "--routing-mode",
                "fixed",
                "--strategy",
                "planner",
                "--non-interactive",
                "--acceptance-file",
                str(acceptance),
            ]
        )
        == 0
    )
    emitted = json.loads(capsys.readouterr().out)
    with SQLiteStore(database) as store:
        run = store.get("graph_run_v2", emitted["run_id"], GraphRunRecord)
        approval = store.get("approval_v2", run.promotion_approval_id, ApprovalRecord)
    assert (
        cli.main(
            [
                "approvals",
                "approve",
                approval.id,
                "--request-digest",
                approval.request_digest,
            ]
        )
        == 0
    )
    capsys.readouterr()
    acceptance.write_text("{}")  # Original input is not authoritative on replay.
    if weaken_harness:
        path = repository / ".fleet/project.json"
        config = json.loads(path.read_text())
        config["commands"]["goal-check"]["argv"][-1] = "assert True"
        path.write_text(json.dumps(config))
    code = cli.main(
        [
            "promote",
            run.id,
            "--patch-digest",
            run.parent_candidate_digest,
        ]
    )
    assert (code == 0) == (not weaken_harness)
    capsys.readouterr()
    assert (repository / "c.txt").read_text() == ("c-before\n" if weaken_harness else "c-after\n")
