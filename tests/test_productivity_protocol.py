from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from ai_employee.productivity_evaluation import (
    AcceptanceCriterion,
    RegressionCheck,
    TaskClass,
    TaskIdentity,
)
from ai_employee.productivity_protocol import collect_protocol
from ai_employee.serialization import canonical_json

MANIFEST = Path("examples/productivity/protocols.json")
PRODUCER = Path("tests/fixtures/productivity_protocol_producer.py").resolve()


def _task(path: Path) -> Path:
    task = TaskIdentity(
        benchmark="fixture",
        benchmark_version="v1",
        task_id="case-1",
        task_version="v1",
        repository="fixture/repository",
        baseline_commit="a" * 40,
        task_class=TaskClass.BUG_FIX,
        acceptance_criteria=(
            AcceptanceCriterion(
                id="acceptance",
                description="fixture acceptance",
                authority="fixture-pytest",
            ),
        ),
        regression_checks=(
            RegressionCheck(
                id="regression",
                description="fixture regression",
                authority="fixture-pytest",
            ),
        ),
    )
    path.write_text(canonical_json(task) + "\n", encoding="utf-8")
    return path


def _command(mode: str = "valid") -> list[str]:
    return [
        sys.executable,
        str(PRODUCER),
        "--task",
        "{task}",
        "--repository",
        "{repository}",
        "--output",
        "{output}",
        "--protocol",
        "{protocol}",
        "--mode",
        mode,
    ]


def _collect(tmp_path: Path, mode: str = "valid") -> Path:
    repository = tmp_path / "repository"
    repository.mkdir(exist_ok=True)
    return collect_protocol(
        manifest_path=MANIFEST,
        protocol_id="codex-direct",
        task_path=_task(tmp_path / "task.json"),
        repository=repository,
        output_root=tmp_path,
        timeout=10,
        network="disabled",
        arm_command=_command(mode),
    )


def test_collect_protocol_crosses_process_boundary_and_atomically_records(tmp_path: Path) -> None:
    command_path = _collect(tmp_path)
    artifact_directory = command_path.parent
    assert {item.name for item in artifact_directory.iterdir()} == {
        "command.json",
        "acceptance.json",
        "regression.json",
        "result-bundle.json",
    }
    command_bytes = command_path.read_bytes()
    command = json.loads(command_bytes)
    assert (
        command_bytes
        == (
            json.dumps(command, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode()
    )
    assert command["exit_code"] == 0
    assert command["network"] == "disabled"
    assert command["cwd"] == str((tmp_path / "repository").resolve())
    assert all("{" not in item for item in command["argv"])
    for name in ("acceptance.json", "regression.json", "result-bundle.json"):
        assert (
            command["content_digests"][name]
            == hashlib.sha256((artifact_directory / name).read_bytes()).hexdigest()
        )


@pytest.mark.parametrize(
    ("mode", "message"),
    (
        ("bad-evidence", "evidence digest"),
        ("extra", "exactly acceptance"),
        ("missing", "exactly acceptance"),
        ("noncanonical-bundle", "not canonical"),
        ("wrong-arm", "protocol treatments"),
        ("wrong-task", "supplied task"),
    ),
)
def test_collect_protocol_rejects_untrusted_producer_outputs(
    tmp_path: Path, mode: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _collect(tmp_path, mode)
    assert not (tmp_path / "artifacts" / "codex-direct").exists()


def test_collect_protocol_rejects_nonzero_and_overwrite(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exited with 7"):
        _collect(tmp_path, "nonzero")
    assert not (tmp_path / "artifacts" / "codex-direct").exists()

    _collect(tmp_path)
    with pytest.raises(FileExistsError, match="overwrite"):
        _collect(tmp_path)


def test_collect_protocol_rejects_network_mismatch_before_execution(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    with pytest.raises(ValueError, match="network policies"):
        collect_protocol(
            manifest_path=MANIFEST,
            protocol_id="codex-direct",
            task_path=_task(tmp_path / "task.json"),
            repository=repository,
            output_root=tmp_path,
            timeout=10,
            network="enabled",
            arm_command=_command(),
        )


def test_collect_protocol_rejects_manifest_path_escape(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["protocols"][0]["artifacts"] = "../escaped/command.json"
    escaped_manifest = tmp_path / "protocols.json"
    escaped_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    repository = tmp_path / "repository"
    repository.mkdir()
    with pytest.raises(ValueError, match="contained relative"):
        collect_protocol(
            manifest_path=escaped_manifest,
            protocol_id="ablation-no-planning",
            task_path=_task(tmp_path / "task.json"),
            repository=repository,
            output_root=tmp_path,
            timeout=10,
            network="disabled",
            arm_command=_command(),
        )
