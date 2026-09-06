"""No credentials or models: exercise the kernel admission boundary in Docker."""

from __future__ import annotations

import pytest

from ai_employee.isolated_worker import (
    DockerCandidate,
    IsolatedWorkerProfile,
    NativeProcessBudgetExceeded,
)
from tests.test_issue81_isolation import (
    IMAGE,
    NATIVE_UNAVAILABLE,
    Cancellation,
    docker_test,
    repository,
)


@docker_test
@pytest.mark.parametrize("method", ["subprocess", "fork", "threads", "untraced", "tamper"])
def test_admission_boundary(tmp_path, method):
    setup = {
        "subprocess": "import subprocess; "
        "spawn=lambda: subprocess.run(['python','-c','pass'],check=True)",
        "fork": "import os\ndef spawn():\n p=os.fork()\n if p==0: os._exit(0)\n os.waitpid(p,0)\n",
        "threads": "import threading,subprocess\ndef spawn():\n "
        "t=threading.Thread(target=lambda:subprocess.run(['python','-c','pass'],check=True))\n "
        "t.start(); t.join()\n",
        "untraced": "import os,ctypes,platform\ndef spawn():\n "
        "c=ctypes.CDLL(None); n=56 if platform.machine()=='x86_64' else 220\n "
        "p=c.syscall(n,0x800000|17,0,0,0,0)\n assert p>=0\n "
        "if p==0: os._exit(0)\n os.waitpid(p,0)\n",
        "tamper": "import subprocess,pathlib\n"
        "for p in pathlib.Path('/tmp').glob('fleet-control-*/usage.json'): p.write_text('{}')\n"
        "spawn=lambda:subprocess.run(['python','-c','pass'],check=True)",
    }[method]
    with DockerCandidate(
        IsolatedWorkerProfile(image=IMAGE),
        repository(tmp_path),
        seconds=30,
        cancellation=Cancellation(),
    ) as candidate:
        code, _, stderr = candidate.run_guarded(
            ("python", "-c", setup + "\nspawn(); spawn()"),
            process_limit=3,
        )
        assert code == 0, stderr.decode()
        assert candidate.native_process_usage["admitted"] == 3
        with pytest.raises(NativeProcessBudgetExceeded):
            candidate.run_guarded(
                (
                    "python",
                    "-c",
                    setup + "\nspawn(); spawn(); open('/work/escaped','w').write('bad')",
                ),
                process_limit=2,
            )
        assert candidate.native_process_usage["admitted"] == 2
        assert candidate.native_process_usage["denied"] is True
        code, _, _ = candidate.run(
            ("python", "-c", "from pathlib import Path; assert not Path('escaped').exists()")
        )
        assert code == 0


@docker_test
def test_threads_are_live_pid_bounded_not_cumulative_processes(tmp_path):
    with DockerCandidate(
        IsolatedWorkerProfile(image=IMAGE),
        repository(tmp_path),
        seconds=30,
        cancellation=Cancellation(),
    ) as candidate:
        code, _, stderr = candidate.run_guarded(
            (
                "python",
                "-c",
                "import threading\nfor _ in range(200):\n "
                "t=threading.Thread(target=lambda:None); t.start(); t.join()",
            ),
            process_limit=1,
        )
        assert code == 0, stderr.decode()
        assert candidate.native_process_usage["admitted"] == 1


@docker_test
def test_guard_death_never_accepts_candidate(tmp_path):
    with (
        DockerCandidate(
            IsolatedWorkerProfile(image=IMAGE),
            repository(tmp_path),
            seconds=30,
            cancellation=Cancellation(),
        ) as candidate,
        pytest.raises(RuntimeError, match="PROCESS_GUARD_FAILED"),
    ):
        candidate.run_guarded(
            ("python", "-c", "import os,signal; os.kill(os.getppid(),signal.SIGKILL)"),
            process_limit=1,
        )


@docker_test
def test_detached_descendants_are_destroyed_before_report(tmp_path):
    with DockerCandidate(
        IsolatedWorkerProfile(image=IMAGE),
        repository(tmp_path),
        seconds=30,
        cancellation=Cancellation(),
    ) as candidate:
        code, _, stderr = candidate.run_guarded(
            (
                "python",
                "-c",
                "import subprocess; "
                "subprocess.Popen(['python','-c','import os,time; os.setsid(); time.sleep(100)'])",
            ),
            process_limit=2,
        )
        assert code == 0, stderr.decode()
        assert candidate.native_process_usage["cleanup"] == "confirmed"


@docker_test
def test_guard_blocks_new_listener_clone3_and_tracer_access(tmp_path):
    with DockerCandidate(
        IsolatedWorkerProfile(image=IMAGE),
        repository(tmp_path),
        seconds=30,
        cancellation=Cancellation(),
    ) as candidate:
        code, _, stderr = candidate.run_guarded(
            (
                "python",
                "-c",
                """
import ctypes as c, errno, os, platform
libc=c.CDLL(None,use_errno=True)
sc,ptrace=(317,101) if platform.machine()=='x86_64' else (277,117)
assert libc.syscall(435,0,0)==-1 and c.get_errno()==errno.ENOSYS
assert libc.syscall(sc,1,8,0)==-1 and c.get_errno()==errno.EPERM
assert libc.syscall(ptrace,16,os.getppid(),0,0)==-1 and c.get_errno()==errno.EPERM
try: open('/proc/%d/mem'%os.getppid(),'rb')
except PermissionError: pass
else: raise AssertionError('supervisor memory readable')
""",
            ),
            process_limit=1,
        )
        assert code == 0, stderr.decode()


@docker_test
def test_real_codex_sandbox_runs_under_guard_without_model(tmp_path):
    from ai_employee.isolated_execution import CODEX_SANDBOX_PROBE, codex_isolated_permission_args

    with DockerCandidate(
        IsolatedWorkerProfile(image=IMAGE),
        repository(tmp_path),
        seconds=30,
        cancellation=Cancellation(),
    ) as candidate:
        code, _, stderr = candidate.run_guarded(
            (
                "codex",
                "sandbox",
                *codex_isolated_permission_args(),
                "--",
                "python",
                "-I",
                "-c",
                CODEX_SANDBOX_PROBE,
            ),
            process_limit=16,
        )
        if NATIVE_UNAVAILABLE:
            assert code != 0 and b"Permission denied" in stderr
        else:
            assert code == 0, stderr.decode()
        assert 1 <= candidate.native_process_usage["admitted"] <= 16


@docker_test
def test_native_startup_fits_budget_without_auth_or_network(tmp_path):
    from ai_employee.isolated_execution import codex_isolated_exec_args

    with DockerCandidate(
        IsolatedWorkerProfile(image=IMAGE),
        repository(tmp_path),
        seconds=20,
        cancellation=Cancellation(),
    ) as candidate:
        code, _, _ = candidate.run_guarded(
            ("timeout", "5", *codex_isolated_exec_args("fixed-test-model", "low")),
            process_limit=128,
            stdin=b"Say ready. Do not use tools.",
        )
        # Deliberately no auth/gateway: transport cannot reach a real model.
        assert code != 0
        assert candidate.native_process_usage["denied"] is False
