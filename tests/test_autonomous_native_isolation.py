"""Opt-in model-free tests against the installed native sandbox, using disposable canaries."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from ai_employee.models import Authority
from ai_employee.native import NativeSandboxProbe, codex_permissions, run_process

pytestmark = pytest.mark.skipif(
    os.environ.get("FLEET_TEST_NATIVE") != "1",
    reason="native isolation tests are explicitly opt-in",
)


def test_native_filesystem_and_offline_network_boundary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    native = NativeSandboxProbe()
    if os.environ.get("FLEET_TEST_EXPECT_NATIVE_UNAVAILABLE") == "1":
        with pytest.raises(ValueError, match="NATIVE_SANDBOX_PREFLIGHT_FAILED"):
            native.preflight(workspace, lambda: False)
        return
    native.preflight(workspace, lambda: False)
    host_secret = tmp_path / "disposable-secret"
    host_secret.write_text("disposable canary")
    inputs = workspace / ".fleet-inputs"
    inputs.mkdir()
    (inputs / "original").write_text("immutable upstream")
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        program = f"""
from pathlib import Path
import socket
Path('result').write_text('autonomous work')
assert Path('.fleet-inputs/original').read_text() == 'immutable upstream'
for operation in (
    lambda: Path({str(host_secret)!r}).read_text(),
    lambda: Path({str(host_secret)!r}).write_text('bad'),
    lambda: Path('.fleet-inputs/original').write_text('bad'),
    lambda: socket.create_connection(('127.0.0.1', {port}), timeout=1),
):
    try:
        operation()
    except (PermissionError, OSError):
        pass
    else:
        raise AssertionError('sandbox boundary bypassed')
"""
        code, _ = run_process(
            (
                native._codex(),
                *codex_permissions(workspace, Authority()),
                "sandbox",
                "--permission-profile",
                "fleet-worker",
                "--cd",
                str(workspace),
                "--",
                "/usr/bin/python3",
                "-I",
                "-c",
                program,
            ),
            workspace,
            10,
            lambda: False,
        )
        assert code == 0
        assert (workspace / "result").read_text() == "autonomous work"
        assert host_secret.read_text() == "disposable canary"
        assert (inputs / "original").read_text() == "immutable upstream"
    finally:
        listener.close()
