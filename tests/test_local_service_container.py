"""Opt-in local service environment qualification, without model access."""

import json
import os
from pathlib import Path

import pytest

from ai_employee.container import ContainerModel
from ai_employee.isolated_worker import IsolatedWorkerProfile
from ai_employee.models import Authority
from ai_employee.native import codex_permissions

pytestmark = pytest.mark.skipif(
    not os.environ.get("FLEET_TEST_SERVICE_IMAGE"),
    reason="explicit database fixture image required",
)


def configured(storage=128):
    return ContainerModel(
        IsolatedWorkerProfile(
            image=os.environ["FLEET_TEST_SERVICE_IMAGE"], local_service_storage_mb=storage
        )
    )


def native(candidate, *argv, authority=None):
    return (
        "codex",
        *codex_permissions(
            Path("/work"),
            authority if authority is not None else Authority(),
            local_service_storage_mb=candidate.profile.local_service_storage_mb,
        ),
        "sandbox",
        "--permission-profile",
        "fleet-worker",
        "--cd",
        "/work",
        "--",
        *argv,
    )


def test_services_restart_between_commands_and_fresh_verification(tmp_path):
    model = configured()
    workspace = tmp_path / "work"
    workspace.mkdir()
    with model._candidate(workspace, 180, lambda: False, models=False) as candidate:
        model._native_probe(candidate)
        for expected in (1, 2):
            code, stdout, stderr = candidate.run_guarded(
                native(candidate, "fleet-service-tests", "--expected", str(expected))
            )
            assert code == 0, stderr.decode(errors="replace")[-2000:]
            result = json.loads(stdout)
            assert result["application_counter"] == expected
            print(result)
            assert candidate.native_completion["cleanup"] == "confirmed"
        model._copy_workspace(candidate, workspace)
    assert not any(workspace.iterdir())
    passed, output = model.check(
        ("fleet-service-tests", "--expected", "1"), workspace, 90, lambda: False
    )
    assert passed, str(output)[-2000:]


@pytest.mark.parametrize("hosts", [(), ("example.com",)])
def test_runtime_storage_is_bounded_and_network_does_not_escape(tmp_path, hosts):
    model = configured(16)
    authority = Authority(network_hosts=hosts)
    work = tmp_path / "work"
    peer_work = tmp_path / "peer"
    work.mkdir()
    peer_work.mkdir()
    with (
        model._candidate(work, 90, lambda: False, models=False, authority=authority) as candidate,
        model._candidate(peer_work, 90, lambda: False, models=False) as peer,
    ):
        model._native_probe(candidate, authority)
        # An outer-container listener must not become reachable through loopback.
        candidate._docker(
            "exec",
            "-d",
            candidate.name,
            "python",
            "-m",
            "http.server",
            "45111",
            "--bind",
            "127.0.0.1",
            "--directory",
            "/tmp",
        )
        peer._docker(
            "exec",
            "-d",
            peer.name,
            "python",
            "-m",
            "http.server",
            "45112",
            "--bind",
            "127.0.0.1",
            "--directory",
            "/tmp",
        )
        for owner, port in ((candidate, 45111), (peer, 45112)):
            owner._docker(
                "exec",
                owner.name,
                "python",
                "-I",
                "-c",
                "import socket,time\nfor _ in range(100):\n"
                f" try: socket.create_connection(('127.0.0.1',{port}),timeout=.1).close(); break\n"
                " except OSError: time.sleep(.05)\nelse: raise RuntimeError('canary not ready')",
            )
        script = """
import errno, http.client, os, socket, urllib.parse
from pathlib import Path
root=Path('/fleet-runtime')
fs=os.statvfs(root)
assert fs.f_blocks * fs.f_frsize <= 16*1024**2
try:
    with (root/'full').open('wb') as f:
        for _ in range(17): f.write(b'x'*1024**2)
except OSError as e: assert e.errno == errno.ENOSPC
else: raise AssertionError('runtime storage exceeded bound')
(root/'full').unlink()
executable=root/'executable'
executable.write_text('#!/bin/sh'+chr(10)+'exit 0'+chr(10))
executable.chmod(0o700)
import subprocess
try: subprocess.run([str(executable)],check=True)
except PermissionError: pass
else: raise AssertionError('runtime data filesystem permits execution')
executable.unlink()
for target in [('127.0.0.1',45111), ('127.0.0.1',45112), ('192.0.2.1',9)]:
    try: socket.create_connection(target,timeout=.3)
    except OSError: pass
    else: raise AssertionError('direct namespace escape')
proxy=urllib.parse.urlparse(os.environ.get('HTTP_PROXY') or os.environ['http_proxy'])
for target in ['http://127.0.0.1:45111/', 'http://fleet-denied.invalid/']:
    c=http.client.HTTPConnection(proxy.hostname,proxy.port,timeout=3)
    c.request('GET',target)
    assert c.getresponse().status == 403
    c.close()
try: socket.socket(socket.AF_UNIX)
except OSError: pass
else: raise AssertionError('unsupported Unix socket unexpectedly available')
"""
        code, _, stderr = candidate.run_guarded(
            native(candidate, "python", "-I", "-c", script, authority=authority)
        )
        assert code == 0, stderr.decode(errors="replace")[-2000:]


def test_failed_work_retains_only_source_and_not_runtime_data(tmp_path):
    model = configured()
    events = []

    def retain(event):
        root = Path(event["workspace"])
        events.append(
            {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob("*") if p.is_file()}
        )

    with (
        pytest.raises(ValueError, match="fixture failure"),
        model._candidate(
            tmp_path, 90, lambda: False, models=False, retain_partial=retain
        ) as candidate,
    ):
        model._native_probe(candidate)
        code, _, stderr = candidate.run_guarded(
            native(
                candidate,
                "python",
                "-I",
                "-c",
                "from pathlib import Path; Path('/fleet-runtime/data').write_text('runtime'); "
                "Path('/work/source.txt').write_text('unfinished'); raise SystemExit(7)",
            ),
            phase="model",
        )
        assert code == 7, stderr
        raise ValueError("fixture failure")
    assert events == [{"source.txt": "unfinished"}]
    # Partial capture never overwrites the live retry workspace.
    assert not (tmp_path / "source.txt").exists()
    assert not (tmp_path / "data").exists()


def test_omitted_service_capability_keeps_offline_socket_denial(tmp_path):
    model = configured(None)
    with model._candidate(tmp_path, 60, lambda: False, models=False) as candidate:
        model._native_probe(candidate)
        script = (
            "import socket; from pathlib import Path\n"
            "assert not Path('/fleet-runtime').exists()\n"
            "try: socket.socket(socket.AF_INET)\n"
            "except PermissionError: pass\n"
            "else: raise AssertionError('offline sockets unexpectedly enabled')\n"
        )
        code, _, stderr = candidate.run_guarded(native(candidate, "python", "-I", "-c", script))
        assert code == 0, stderr
