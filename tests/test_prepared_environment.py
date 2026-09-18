"""Prepared image contract and effective readiness; no model calls."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from ai_employee.container import ContainerModel
from ai_employee.models import Authority
from ai_employee.native import codex_permissions
from ai_employee.product_capabilities import DEPENDENCY_ENVIRONMENT, NATIVE_PATH


def test_wrapper_discovery_is_projected_without_additional_filesystem_grants(tmp_path):
    args = codex_permissions(tmp_path, Authority())
    assert NATIVE_PATH in " ".join(args)
    assert DEPENDENCY_ENVIRONMENT["path"] == NATIVE_PATH
    filesystem = next(a for a in args if a.startswith("permissions.fleet-worker.filesystem="))
    assert '":minimal"="read"' in filesystem
    assert '":root"' not in filesystem
    assert '"/usr/local/lib"="write"' not in filesystem


@pytest.mark.parametrize(
    ("code", "error"),
    [(1, "NATIVE_SANDBOX_PREFLIGHT_FAILED"), (78, "NATIVE_RUNTIME_PROCFS_UNAVAILABLE")],
)
def test_effective_readiness_failure_is_not_reported_as_available(code, error):
    candidate = Mock()
    candidate.run_guarded.return_value = (code, b"", b"")
    with pytest.raises(ValueError, match=error):
        ContainerModel._native_probe(candidate)
    command = candidate.run_guarded.call_args.args[0]
    assert "sandbox" in command
    assert "/proc/self/status" in command[-1]
    assert "/proc/self/smaps" in command[-1]
    assert "/proc/sys" in command[-1]
    compile(command[-1], "native-preflight", "exec")


def test_restricted_procfs_patch_is_exact_and_fails_on_changed_source(tmp_path):
    import subprocess
    import sys

    patcher = Path(__file__).parents[1] / "docker/restricted-procfs.py"
    source = tmp_path / "bubblewrap.c"
    source.write_text('mount ("proc", dest_path, "proc", MS_NOSUID | MS_NOEXEC | MS_NODEV, NULL)')
    subprocess.run([sys.executable, str(patcher), str(source)], check=True)
    assert '"subset=pid"' in source.read_text()
    failed = subprocess.run([sys.executable, str(patcher), str(source)], capture_output=True)
    assert failed.returncode != 0
