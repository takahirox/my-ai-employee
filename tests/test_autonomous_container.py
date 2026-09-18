"""Opt-in disposable namespace/native tests. No model access or real credentials."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


def test_unlimited_invocation_cancellation_removes_descendants(tmp_path: Path) -> None:
    workspace = tmp_path / "worker"
    workspace.mkdir()
    cancelled = False
    with configured()._candidate(workspace, None, lambda: cancelled, models=False) as candidate:
        assert candidate.deadline is None

        def cancel(_size):
            nonlocal cancelled
            cancelled = True

        with pytest.raises(TimeoutError):
            candidate.run_guarded(
                ("python", "-I", "-c", "import time; time.sleep(30)"),
                process_limit=100,
                supervise=cancel,
            )
        assert subprocess.run(["docker", "inspect", candidate.name], capture_output=True).returncode


def test_controller_sigkill_reaps_unlimited_namespace_gateway_and_network(tmp_path: Path) -> None:
    workspace = tmp_path / "worker"
    workspace.mkdir()
    ready = tmp_path / "ready.json"
    script = """
import json,os,time,sys
from pathlib import Path
from ai_employee.container import ContainerModel
from ai_employee.isolated_worker import IsolatedWorkerProfile
from ai_employee.models import Authority
model=ContainerModel(IsolatedWorkerProfile(image=os.environ['FLEET_TEST_DOCKER_IMAGE']))
workspace,ready=map(Path,sys.argv[1:])
with model._candidate(workspace,None,lambda:False,models=False,
                      authority=Authority(network_hosts=('example.com',))) as candidate:
    candidate._docker('exec','-d','--user','1000:1000',candidate.name,'python','-I','-c',
                      'import time; time.sleep(600)')
    ready.write_text(json.dumps([candidate.name,candidate.proxy,candidate.network]))
    time.sleep(600)
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", script, str(workspace), str(ready)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    names = []
    try:
        until = time.monotonic() + 45
        while not ready.exists() and owner.poll() is None and time.monotonic() < until:
            time.sleep(0.1)
        assert ready.exists(), "test controller did not create disposable resources"
        names = json.loads(ready.read_text())
        owner.kill()
        owner.wait(timeout=5)
        until = time.monotonic() + 40
        while time.monotonic() < until:
            present = [
                subprocess.run(
                    ["docker", kind, "inspect", name], capture_output=True, timeout=5
                ).returncode
                == 0
                for kind, name in zip(("container", "container", "network"), names, strict=True)
            ]
            if not any(present):
                break
            time.sleep(0.2)
        assert not any(present), f"owned resources survived controller loss: {present}"
        configured().reconcile(tmp_path)
        # Reconciliation is repeatable and confirms the durable ledger after EOF cleanup.
        configured().reconcile(tmp_path)
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        assert owner.stderr
        owner.stderr.close()
        for kind, name in zip(("container", "container", "network"), names, strict=False):
            subprocess.run(
                ["docker", kind, "rm", *(["-f"] if kind == "container" else []), name],
                capture_output=True,
                timeout=15,
            )


def test_safe_symlinks_cross_real_container_and_candidate_boundaries(tmp_path: Path) -> None:
    from ai_employee.candidates import Candidates

    workspace = tmp_path / "worker"
    (workspace / ".windsurf/rules").mkdir(parents=True)
    (workspace / "AGENTS.md").write_text("instructions")
    (workspace / "CLAUDE.md").symlink_to("AGENTS.md")
    (workspace / ".windsurf/rules/chatwoot.md").symlink_to("../../AGENTS.md")
    model = configured()
    with model._candidate(workspace, 45, lambda: False, models=False) as candidate:
        program = (
            "from pathlib import Path; import os; "
            "assert os.readlink('CLAUDE.md')=='AGENTS.md'; "
            "assert os.readlink('.windsurf/rules/chatwoot.md')=='../../AGENTS.md'; "
            "assert Path('.windsurf/rules/chatwoot.md').read_text()=='instructions'; "
            "Path('new.txt').write_text('new artifact'); "
            "Path('new-link').symlink_to('new.txt')"
        )
        candidate._docker(
            "exec", "--user", "1000:1000", candidate.name, "python", "-I", "-c", program
        )
        model._copy_workspace(candidate, workspace)
    assert (workspace / "new-link").read_text() == "new artifact"
    assert os.readlink(workspace / ".windsurf/rules/chatwoot.md") == "../../AGENTS.md"
    candidates = Candidates(tmp_path / "objects")
    tree = candidates.capture(workspace)
    candidates.materialize(tree, tmp_path / "restored")
    assert (tmp_path / "restored/CLAUDE.md").is_symlink()
    assert (tmp_path / "restored/new-link").read_text() == "new artifact"
