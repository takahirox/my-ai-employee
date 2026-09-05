from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from ai_employee.isolated_worker import DockerCandidate, IsolatedWorkerProfile, candidate_archive

IMAGE = os.environ.get("FLEET_TEST_DOCKER_IMAGE")
docker_test = pytest.mark.skipif(not IMAGE, reason="explicit offline Docker integration opt-in")


class Cancellation:
    stop = False

    def cancelled(self) -> bool:
        return self.stop


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "solution.py").write_text("def value(): return 0\n")
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    (root / "untracked-secret").write_text("must not leave host")
    return root


def test_archive_excludes_host_git_and_untracked_secrets(tmp_path: Path) -> None:
    root = repository(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(candidate_archive(root, 1000))) as archive:
        assert archive.getnames() == ["solution.py"]
        assert archive.getmember("solution.py").uid == 1000
    with pytest.raises(ValueError, match="byte limit"):
        candidate_archive(root, 1)


def test_archive_rejects_tracked_symlink(tmp_path: Path) -> None:
    root = repository(tmp_path)
    (root / "solution.py").unlink()
    (root / "solution.py").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlinks"):
        candidate_archive(root, 1000)


def test_mutable_image_tag_is_rejected() -> None:
    with pytest.raises(ValueError, match="immutable"):
        IsolatedWorkerProfile(image="python:latest")


def test_native_exec_selects_permissions_via_config_not_sandbox_only_flag():
    from ai_employee.isolated_execution import codex_isolated_exec_args

    args = codex_isolated_exec_args("fixed-test-model", "low")
    assert "--permission-profile" not in args
    assert 'default_permissions="fleet-isolated"' in args
    assert "--sandbox" not in args
    assert "features.multi_agent=false" in args
    assert 'web_search="disabled"' in args
    assert args.index("--ignore-user-config") > args.index("exec")
    assert args[-1] == "-"


@pytest.mark.parametrize("stderr_only", [False, True])
def test_usage_limit_stops_graph_without_retry_reset_or_candidate_submission(
    tmp_path, monkeypatch, stderr_only
):
    from ai_employee import isolated_execution
    from tests.test_work_orchestration_v2 import Channel, worker_request

    calls, stopped, closed = [], [], []

    class FakeCandidate:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def run(self, argv, **kwargs):
            calls.append(argv)
            if argv == ("codex", "--version"):
                return 0, b"", b""
            if argv[1] == "sandbox":
                return 0, b"", b""
            if stderr_only:
                return 1, b"", b"You have hit your usage limit."
            kwargs["observe"]({"type": "turn.failed", "error": {"message": "usage_limit_reached"}})
            pytest.fail("usage limit must terminate the invocation")

        def capture(self, *args):
            pytest.fail("never submit a candidate after a usage limit")

    monkeypatch.setattr(isolated_execution, "DockerCandidate", FakeCandidate)
    adapter = isolated_execution.IsolatedCodexWorker(
        tmp_path,
        IsolatedWorkerProfile(image="sha256:" + "0" * 64, auth_file="/explicit/dummy.json"),
        model="fixed-test-model",
        effort="low",
        cancellation=Cancellation(),
        seconds=2.0,
        commands=(),
        persist=lambda *_: "0" * 64,
        on_usage_limit=lambda: stopped.append(True),
    )
    request = worker_request().model_copy(update={"remaining_budgets": {"artifact_bytes": 100_000}})
    result = adapter.propose(request, Channel())
    assert result.failure.code.value == "BUDGET_EXCEEDED"
    assert "USAGE_LIMIT" in result.failure.message
    assert stopped == [True] and closed == [True] and len(calls) == 3
    assert result.proposals == () and result.usage is None


def test_missing_explicit_auth_stops_before_container_or_artifact_creation(tmp_path, monkeypatch):
    from ai_employee import isolated_execution
    from tests.test_work_orchestration_v2 import Channel, worker_request

    def forbidden(*args, **kwargs):
        pytest.fail("missing auth must not start a container or persist a model prompt")

    monkeypatch.setattr(isolated_execution, "DockerCandidate", forbidden)
    adapter = isolated_execution.IsolatedCodexWorker(
        tmp_path,
        IsolatedWorkerProfile(image="sha256:" + "0" * 64),
        model="fixed-test-model",
        effort="low",
        cancellation=Cancellation(),
        seconds=2.0,
        commands=(),
        persist=forbidden,
    )
    request = worker_request().model_copy(update={"remaining_budgets": {"artifact_bytes": 100_000}})
    result = adapter.propose(request, Channel())
    assert result.failure and "auth_file" in result.failure.message
    assert result.proposals == ()


@docker_test
def test_real_codex_cli_flags_are_supported_without_model_or_credentials(tmp_path):
    from ai_employee.isolated_execution import (
        CODEX_SANDBOX_PROBE,
        codex_isolated_exec_args,
        codex_isolated_permission_args,
    )

    with DockerCandidate(
        IsolatedWorkerProfile(image=IMAGE),
        repository(tmp_path),
        seconds=45,
        cancellation=Cancellation(),
    ) as candidate:
        code, stdout, stderr = candidate.run(
            (*codex_isolated_exec_args("fixed-test-model", "low")[:-1], "--help")
        )
        assert code == 0, stderr.decode()
        assert b"--ephemeral" in stdout and b"--sandbox" in stdout and b"--json" in stdout
        code, stdout, stderr = candidate.run(("codex", "sandbox", "--help"))
        assert code == 0, stderr.decode()
        sandbox_help = stdout.decode()
        code, _, stderr = candidate.run(
            (
                "python",
                "-I",
                "-c",
                "from pathlib import Path; p=Path('/home/fleet/.codex'); "
                "p.mkdir(exist_ok=True); (p/'auth.json').write_text('{}')",
            )
        )
        assert code == 0, stderr.decode()
        code, stdout, stderr = candidate.run(
            (
                "codex",
                "sandbox",
                *codex_isolated_permission_args(),
                "--",
                "python",
                "-I",
                "-c",
                CODEX_SANDBOX_PROBE,
            )
        )
        assert code == 0, sandbox_help + stderr.decode()


@docker_test
def test_real_isolated_edit_run_observe_repair_and_git_capture(tmp_path: Path) -> None:
    root = repository(tmp_path)
    profile = IsolatedWorkerProfile(image=IMAGE)
    with DockerCandidate(profile, root, seconds=45, cancellation=Cancellation()) as candidate:
        config = json.loads(candidate._docker("inspect", candidate.name))[0]
        host = config["HostConfig"]
        assert host["PidsLimit"] == profile.pids_limit
        assert host["Memory"] == profile.memory_mb * 1024**2
        assert host["NanoCpus"] == int(profile.cpus * 1_000_000_000)
        assert "no-new-privileges" in host["SecurityOpt"]
        assert host["NetworkMode"] == "none" and not host.get("Binds")
        first, _, _ = candidate.run(
            ("python", "-I", "-c", "exec(open('solution.py').read()); assert value()==42")
        )
        assert first != 0
        code, _, _ = candidate.run(
            (
                "python",
                "-I",
                "-c",
                "from pathlib import Path; "
                "Path('solution.py').write_text('def value(): return 42\\n'); "
                "exec(open('solution.py').read()); assert value()==42; "
                "assert not Path('untracked-secret').exists()",
            )
        )
        assert code == 0
        paths, patch = candidate.capture()
        name = candidate.name
    assert paths == ("solution.py",) and b"+def value(): return 42" in patch
    assert (root / "solution.py").read_text() == "def value(): return 0\n"
    assert (
        subprocess.run(["docker", "inspect", name], capture_output=True, check=False).returncode
        != 0
    )


@docker_test
def test_model_gateway_rejects_non_provider_connect_without_external_request(tmp_path):
    auth = tmp_path / "dummy-auth.json"
    auth.write_text("{}")
    profile = IsolatedWorkerProfile(image=IMAGE, auth_file=str(auth))
    with DockerCandidate(
        profile, repository(tmp_path), seconds=45, cancellation=Cancellation()
    ) as candidate:
        code, _, stderr = candidate.run(
            (
                "python",
                "-I",
                "-c",
                "import os,socket,urllib.parse; "
                "p=urllib.parse.urlsplit(os.environ['HTTPS_PROXY']); "
                "s=socket.create_connection((p.hostname,p.port),timeout=3); "
                "s.sendall(b'CONNECT example.invalid:443 HTTP/1.1\\r\\n"
                "Host: example.invalid:443\\r\\n\\r\\n'); "
                "assert b'403' in s.recv(1024).split(b'\\r\\n')[0]; s.close()",
            )
        )
        assert code == 0, stderr.decode()


@docker_test
@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_or_cancel_removes_container_and_descendants(tmp_path: Path, cancel: bool) -> None:
    root = repository(tmp_path)
    cancellation = Cancellation()
    profile = IsolatedWorkerProfile(image=IMAGE)
    with DockerCandidate(profile, root, seconds=45, cancellation=cancellation) as candidate:
        if cancel:
            cancellation.stop = True
        else:
            import time

            candidate.deadline = time.monotonic() + 0.3
        with pytest.raises(TimeoutError):
            candidate.run(
                (
                    "python",
                    "-I",
                    "-c",
                    "import subprocess,time; "
                    "subprocess.Popen(['python','-c','import time; time.sleep(100)']); "
                    "time.sleep(100)",
                )
            )
        name = candidate.name
    assert (
        subprocess.run(["docker", "inspect", name], capture_output=True, check=False).returncode
        != 0
    )


@docker_test
def test_cli_native_iteration_and_fresh_independent_verification(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from ai_employee import cli
    from ai_employee.domain.v2 import WorkerResult
    from ai_employee.storage import SQLiteStore
    from tests.test_cli_graph_e2e import _fixture

    root, operator, database, _ = _fixture(tmp_path, task_review=False)
    monkeypatch.delenv("FLEET_DB", raising=False)
    monkeypatch.setattr(cli, "resolve_database_path", lambda *_args, **_kwargs: database)
    path = root / ".fleet/project.json"
    harness = json.loads(path.read_text())
    check = (
        "python",
        "-I",
        "-c",
        "from pathlib import Path; assert Path('c.txt').read_text()=='c-after\\n'",
    )
    harness["commands"] = {"parent-test": {"argv": check}}
    harness["worker"]["isolated_workspace_tools"] = True
    harness["budgets"]["wall_seconds"] = 120.0
    path.write_text(json.dumps(harness))
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-qm",
            "isolated fixture",
        ],
        check=True,
    )
    auth = tmp_path / "dummy-auth.json"
    auth.write_text("{}")  # synthetic fixture; no real credentials and no model calls
    config = json.loads(operator.read_text())
    config["isolated_worker"] = {"image": IMAGE, "auth_file": str(auth)}
    operator.write_text(json.dumps(config))
    original = DockerCandidate.run
    seen: list[str] = []

    def scripted_native(self, argv, **kwargs):
        if argv[:2] == ("codex", "sandbox"):
            return original(self, argv, **kwargs)
        if argv[0] != "codex":
            seen.append(self.name)
            return original(self, argv, **kwargs)
        if argv == ("codex", "--version"):
            return 0, b"scripted-native", b""
        failed, _, _ = original(self, check)
        assert failed != 0
        repaired, _, _ = original(
            self,
            (
                "python",
                "-I",
                "-c",
                "from pathlib import Path; Path('c.txt').write_text('c-after\\n')",
            ),
        )
        assert repaired == 0
        passed, _, _ = original(self, check)
        assert passed == 0
        # Attempt to leave a daemon: capture must stop it before freezing the patch.
        original(
            self,
            (
                "python",
                "-I",
                "-c",
                "import subprocess; "
                "subprocess.Popen(['python','-c','import time; time.sleep(100)'], "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
            ),
        )
        seen.append(self.name)
        kwargs["observe"](
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "id": "first", "exit_code": failed},
            }
        )
        kwargs["observe"](
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "id": "repair", "exit_code": passed},
            }
        )
        return 0, b"scripted", b""

    monkeypatch.setattr(DockerCandidate, "run", scripted_native)
    result = cli.main(
        [
            "work",
            "change c.txt",
            "--repo",
            str(root),
            "--operator-config",
            str(operator),
            "--routing-mode",
            "fixed",
            "--strategy",
            "planner",
            "--non-interactive",
        ]
    )
    emitted = json.loads(capsys.readouterr().out)
    assert result == 0 and emitted["status"] == "ready_to_promote", emitted
    assert (root / "c.txt").read_text() == "c-before\n"
    assert len(set(seen)) >= 2  # worker and independent checks use different containers
    with SQLiteStore(database) as store:
        results = store.list_records("worker_result_v2", WorkerResult)
        native = next(item for item in results if item.proposals)
        assert native.stdout_artifact_digest and native.usage is None
        assert native.resource_usage["local_activity"][0]["exit_code"] != 0
    for name in set(seen):
        assert subprocess.run(["docker", "inspect", name], capture_output=True).returncode != 0
