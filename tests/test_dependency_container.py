"""Opt-in prepared-image dependency qualification, without model credentials."""

import os
from pathlib import Path

import pytest

from ai_employee.container import ContainerModel
from ai_employee.isolated_worker import IsolatedWorkerProfile
from ai_employee.models import Authority
from ai_employee.native import codex_permissions

pytestmark = pytest.mark.skipif(
    not os.environ.get("FLEET_TEST_DEPENDENCY_IMAGE"),
    reason="explicit prepared fixture image required",
)


def test_prepared_dependencies_in_worker_and_independent_check(tmp_path):
    model = ContainerModel(IsolatedWorkerProfile(image=os.environ["FLEET_TEST_DEPENDENCY_IMAGE"]))
    workspace = tmp_path / "work"
    workspace.mkdir()
    with model._candidate(workspace, 60, lambda: False, models=False) as candidate:
        model._native_probe(candidate)
        code, _, stderr = candidate.run_guarded(
            (
                "codex",
                *codex_permissions(Path("/work"), Authority()),
                "sandbox",
                "--permission-profile",
                "fleet-worker",
                "--cd",
                "/work",
                "--",
                "/bin/sh",
                "-c",
                "fleet-image-tests",
            )
        )
        assert code == 0, stderr.decode(errors="replace")[-2000:]
        model._copy_workspace(candidate, workspace)
    assert not any(workspace.iterdir())
    passed, output = model.check(
        ("/bin/sh", "-c", "fleet-image-tests"), workspace, 60, lambda: False
    )
    assert passed, str(output)[-2000:]
