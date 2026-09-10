"""Opt-in disposable namespace/native tests. No model access or real credentials."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from ai_employee.container import ContainerModel
from ai_employee.isolated_worker import IsolatedWorkerProfile
from ai_employee.models import Authority
from ai_employee.native import codex_permissions

pytestmark = pytest.mark.skipif(
    not os.environ.get("FLEET_TEST_DOCKER_IMAGE"), reason="explicit immutable test image required"
)


def configured() -> ContainerModel:
    return ContainerModel(IsolatedWorkerProfile(image=os.environ["FLEET_TEST_DOCKER_IMAGE"]))


def test_native_processes_stop_before_actual_workspace_capture(tmp_path: Path) -> None:
    workspace = tmp_path / "worker"
    workspace.mkdir()
    (workspace / "initial.txt").write_text("input")
    (workspace / ".fleet-inputs/0").mkdir(parents=True)
    (workspace / ".fleet-inputs/0/upstream").write_text("immutable input")
    model = configured()
    with model._candidate(workspace, 45, lambda: False, models=False) as candidate:
        if os.environ.get("FLEET_TEST_EXPECT_NATIVE_UNAVAILABLE") == "1":
            with pytest.raises(ValueError, match="NATIVE_SANDBOX_PREFLIGHT_FAILED"):
                model._native_probe(candidate)
            return
        model._native_probe(candidate)
        child = (
            "from pathlib import Path; import time; Path('started').write_text('yes'); "
            "time.sleep(1); Path('late').write_text('must not survive')"
        )
        program = (
            "import subprocess,sys,time; from pathlib import Path; "
            "Path('initial.txt').write_text('actual worker modification'); "
            "assert Path('.fleet-inputs/0/upstream').read_text()=='immutable input'; "
            f"subprocess.Popen([sys.executable,'-I','-c',{child!r}],start_new_session=True,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);\n"
            "while not Path('started').exists(): time.sleep(.01)\n"
            "try: Path('.fleet-inputs/0/upstream').write_text('bad')\n"
            "except OSError as error: assert error.errno in (1,13,30)\n"
            "else: raise AssertionError('upstream write was allowed')\n"
        )
        command = (
            "codex",
            *codex_permissions(Path("/work"), Authority()),
            "sandbox",
            "--permission-profile",
            "fleet-worker",
            "--cd",
            "/work",
            "--",
            "/usr/bin/python3",
            "-I",
            "-c",
            program,
        )
        code, _, stderr = candidate.run_guarded(command, process_limit=100)
        assert code == 0, stderr.decode(errors="replace")[-2000:]
        assert candidate.native_process_usage["cleanup"] == "confirmed"
        time.sleep(1.1)
        model._copy_workspace(candidate, workspace)
    assert (workspace / "initial.txt").read_text() == "actual worker modification"
    assert (workspace / "started").exists()
    assert not (workspace / "late").exists()
    assert not (workspace / ".git").exists()
    assert (workspace / ".fleet-inputs/0/upstream").read_text() == "immutable input"


def test_timeout_removes_owned_namespace(tmp_path: Path) -> None:
    workspace = tmp_path / "worker"
    workspace.mkdir()
    model = configured()
    with model._candidate(workspace, 30, lambda: False, models=False) as candidate:
        candidate.deadline = time.monotonic() + 0.3
        with pytest.raises(TimeoutError):
            candidate.run_guarded(
                ("python", "-I", "-c", "import time; time.sleep(30)"), process_limit=100
            )
        assert not candidate.created
        result = subprocess.run(["docker", "inspect", candidate.name], capture_output=True)
        assert result.returncode != 0
