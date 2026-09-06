"""Explicitly opted-in, credential-free benchmark runtime integration tests."""

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from ai_employee import benchmark_adapter as adapter
from ai_employee.isolated_worker import DockerCandidate, IsolatedWorkerProfile
from tests.test_benchmark_adapter import public_request
from tests.test_issue81_isolation import repository

IMAGE = os.environ.get("FLEET_TEST_DOCKER_IMAGE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="explicit offline Docker opt-in required")


def test_task_app_alias_uses_the_same_isolated_workspace(tmp_path):
    root = repository(tmp_path)
    profile = IsolatedWorkerProfile(image=IMAGE)
    with DockerCandidate(
        profile, root, seconds=40, cancellation=SimpleNamespace(cancelled=lambda: False)
    ) as candidate:
        code, _, _ = candidate.run_guarded(
            (
                "python",
                "-I",
                "-c",
                "from pathlib import Path; assert Path('/app').resolve()==Path('/work'); "
                "Path('/app/solution.py').write_text('value = 42\\n'); "
                "assert Path('/work/solution.py').read_text()=='value = 42\\n'",
            ),
            process_limit=4,
        )
        assert code == 0
    assert (root / "solution.py").read_text() == "def value(): return 0\n"


@pytest.mark.parametrize("gateway", [False, True])
def test_controller_crash_cleanup_removes_only_ledger_resources(tmp_path, gateway):
    root = repository(tmp_path)
    ledger, ready = tmp_path / "resources.jsonl", tmp_path / "ready"
    auth = tmp_path / "dummy-auth.json"
    auth.write_text("{}")  # Synthetic; never calls a model.
    script = (
        "import sys,time; from pathlib import Path; from types import SimpleNamespace; "
        "from ai_employee.isolated_worker import DockerCandidate,IsolatedWorkerProfile; "
        "p=IsolatedWorkerProfile(image=sys.argv[4],resource_ledger=sys.argv[2],"
        "auth_file=sys.argv[5] or None); "
        "c=DockerCandidate(p,Path(sys.argv[1]),seconds=60,"
        "cancellation=SimpleNamespace(cancelled=lambda:False)); "
        "c.__enter__(); Path(sys.argv[3]).write_text(c.name); time.sleep(60)"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(root),
            str(ledger),
            str(ready),
            IMAGE,
            str(auth) if gateway else "",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 40
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert ready.exists(), "credential-free controller did not finish startup"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stderr.close()
        adapter.cleanup(tmp_path)
    resources = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(resources) == (6 if gateway else 2)
    for resource in resources:
        assert (
            subprocess.run(
                ["docker", resource["kind"], "inspect", resource["name"]],
                capture_output=True,
                timeout=10,
            ).returncode
            != 0
        )
    adapter.cleanup(tmp_path)  # Idempotent recovery.


@pytest.mark.parametrize("worker_runs_check", [False, True])
def test_product_adapter_through_real_orchestration_with_scripted_native_worker(
    tmp_path, monkeypatch, worker_runs_check
):
    request = public_request(tmp_path)
    request["settings"]["isolated_worker"]["image"] = IMAGE
    (tmp_path / "dummy-auth.json").write_text("{}")
    (tmp_path / "public-checks/smoke.py").write_text(
        "import ast,json,sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n"
        "import execution\n"
        "ast.parse(Path('src/example.py').read_text())\n"
        "json.loads(Path('output/result.json').read_text())\n"
    )
    original = DockerCandidate.run_guarded
    invocations = []

    def scripted(self, argv, **kwargs):
        if argv[:2] != ("codex", "exec"):
            return original(self, argv, **kwargs)
        invocations.append(self.name)
        return original(
            self,
            (
                "python",
                "-I",
                "-c",
                "from pathlib import Path; Path('/app/src/example.py').write_text('value = 1\\n'); "
                "import py_compile; py_compile.compile('/app/src/example.py', doraise=True); "
                "assert list(Path('/app/src/__pycache__').glob('*.pyc')); "
                "Path('/app/output').mkdir(exist_ok=True); "
                "Path('/app/output/result.json').write_text('{}'); "
                + (
                    "import subprocess; subprocess.run("
                    + repr(adapter.make_harness(180.0)["commands"]["smoke"]["argv"])
                    + ", check=True)"
                    if worker_runs_check
                    else "pass"
                ),
            ),
            process_limit=kwargs["process_limit"],
        )

    monkeypatch.setattr(DockerCandidate, "run_guarded", scripted)
    try:
        response = adapter.run(request, tmp_path)
        assert response["outcome"] == "completed", response
        assert response["details"]["status"] == "ready_to_promote", response
        assert len(invocations) == 1
        assert (tmp_path / "workspace/src/example.py").read_text() == "value = 1\n"
        assert (tmp_path / "workspace/output/result.json").read_text() == "{}"
        assert not list((tmp_path / "workspace").rglob("*.pyc"))
    finally:
        adapter.cleanup(tmp_path)


@pytest.mark.parametrize("delete_tracked", [False, True])
def test_capture_excludes_only_untracked_writable_bytecode(tmp_path, delete_tracked):
    root = repository(tmp_path)
    tracked = root / "src/__pycache__/tracked.pyc"
    tracked.parent.mkdir(parents=True)
    tracked.write_bytes(b"original\x00")
    subprocess.run(["git", "-C", str(root), "add", "src"], check=True)
    profile = IsolatedWorkerProfile(image=IMAGE)
    with DockerCandidate(
        profile, root, seconds=40, cancellation=SimpleNamespace(cancelled=lambda: False)
    ) as candidate:
        code, _, _ = candidate.run_guarded(
            (
                "python",
                "-I",
                "-c",
                "from pathlib import Path; "
                "paths=['src/__pycache__/tracked.pyc', 'src/__pycache__/new.pyc', "
                "'src/pkg/__pycache__/new.pyc', 'output/__pycache__/new.pyc', "
                "'output/pkg/__pycache__/new.pyc', 'input/__pycache__/new.pyc', "
                "'.fleet/__pycache__/new.pyc', 'src/__pycache__/keep.py']; "
                "[(Path(p).parent.mkdir(parents=True,exist_ok=True), "
                "Path(p).write_bytes(b'changed\\x00')) for p in paths]; "
                + ("Path(paths[0]).unlink()" if delete_tracked else "pass"),
            ),
            process_limit=4,
        )
        assert code == 0
        paths, _ = candidate.capture(
            tuple(adapter.make_harness(180.0)["paths"].get("generated", ()))
        )
        assert set(paths) == {
            "src/__pycache__/tracked.pyc",
            "input/__pycache__/new.pyc",
            ".fleet/__pycache__/new.pyc",
            "src/__pycache__/keep.py",
        }
    assert tracked.read_bytes() == b"original\x00"
