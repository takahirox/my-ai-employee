import os
import subprocess
import time

import pytest

from ai_employee.run_budget import WallTimeExceeded, wall_budget_scope
from ai_employee.services_v2._common import run_git, run_git_command
from ai_employee.stage_control import StageStopped, bind_stage_cancellation
from ai_employee.storage import SQLiteStore


def test_internal_git_rejects_late_output_and_kills_owned_children(tmp_path):
    run_git_command(("git", "init", "-q", str(tmp_path)))
    with SQLiteStore(tmp_path / "budget.db") as store, wall_budget_scope(store, "run", 0.05):
        # Measure supervised execution, not database creation or checkpoint/close.
        started = time.monotonic()
        with pytest.raises(WallTimeExceeded):
            run_git(tmp_path, "-c", "alias.review-wait=!sleep 0.3; touch late", "review-wait")
        assert time.monotonic() - started < 1.0
    time.sleep(0.4)
    assert not (tmp_path / "late").exists()


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_internal_git_polls_runtime_stage_control_without_model_failure(tmp_path, action):
    run_git_command(("git", "init", "-q", str(tmp_path)))
    started = time.monotonic()

    class Control:
        def cancelled(self):
            return time.monotonic() - started >= 0.05

        def check(self):
            if self.cancelled():
                raise StageStopped(action)

    with bind_stage_cancellation(Control()), pytest.raises(StageStopped) as stopped:
        run_git(tmp_path, "-c", "alias.review-wait=!sleep 0.3; touch late", "review-wait")
    assert stopped.value.action == action
    time.sleep(0.4)
    assert not (tmp_path / "late").exists()


def test_internal_git_preserves_stdin_environment_exit_status_and_output(tmp_path):
    run_git_command(("git", "init", "-q", str(tmp_path)))
    environment = {**os.environ, "FLEET_INTERNAL_GIT_FIXTURE": "yes"}
    result = run_git_command(
        (
            "git",
            "-C",
            str(tmp_path),
            "-c",
            'alias.fixture=!printf "$FLEET_INTERNAL_GIT_FIXTURE:"; cat; exit 7',
            "fixture",
        ),
        input=b"bounded input",
        env=environment,
    )
    assert result.returncode == 7
    assert result.stdout == b"yes:bounded input"
    with pytest.raises(subprocess.CalledProcessError):
        run_git_command(("git", "-C", str(tmp_path), "not-a-command"), check=True)
