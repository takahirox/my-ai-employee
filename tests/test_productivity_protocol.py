from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

import ai_employee.productivity_protocol as productivity_protocol
from ai_employee.productivity_evaluation import (
    AcceptanceCriterion,
    EnvironmentManifest,
    RegressionCheck,
    TaskClass,
    TaskIdentity,
    load_result_bundle,
)
from ai_employee.productivity_protocol import collect_protocol, load_protocol_manifest
from ai_employee.serialization import canonical_digest, canonical_json

MANIFEST = Path("examples/productivity/protocols.json")
PRODUCER = Path("tests/fixtures/productivity_protocol_producer.py").resolve()


@pytest.fixture(autouse=True)
def _trusted_network_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    wrapper = str(Path("/usr/bin/env").resolve(strict=True))
    backend = productivity_protocol._IsolationBackend(
        name="test-deny",
        wrapper_argv=(wrapper,),
        profile_digest=None,
    )
    isolation = productivity_protocol._NetworkIsolation(
        backend=backend,
        probe={
            "ended_at": "2026-01-01T00:00:00.000001Z",
            "execution_argv": (wrapper, "test-probe"),
            "exit_code": 0,
            "payload_argv": ("test-probe",),
            "result": {"denied": True, "errno": 1},
            "started_at": "2026-01-01T00:00:00.000000Z",
            "stderr_digest": hashlib.sha256(b"").hexdigest(),
            "stdout_digest": hashlib.sha256(b"denied").hexdigest(),
        },
    )
    monkeypatch.setattr(
        productivity_protocol,
        "_establish_network_isolation",
        lambda _: isolation,
    )


def _evaluator(check_id: str, mode: str) -> tuple[str, ...]:
    return (
        sys.executable,
        str(PRODUCER),
        "evaluator",
        "--task",
        "{task}",
        "--repository",
        "{repository}",
        "--trial",
        "{trial}",
        "--protocol",
        "{protocol}",
        "--check",
        check_id,
        "--mode",
        mode,
    )


def _task(
    path: Path,
    evaluator_mode: str = "passed",
    acceptance_evaluator: tuple[str, ...] | None = None,
) -> Path:
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
                evaluator_argv=(
                    _evaluator("acceptance", evaluator_mode)
                    if acceptance_evaluator is None
                    else acceptance_evaluator
                ),
            ),
        ),
        regression_checks=(
            RegressionCheck(
                id="regression",
                description="fixture regression",
                authority="fixture-pytest",
                evaluator_argv=_evaluator("regression", evaluator_mode),
            ),
        ),
    )
    path.write_text(canonical_json(task) + "\n", encoding="utf-8")
    return path


def _manifest(path: Path) -> Path:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    protocol = next(item for item in data["protocols"] if item["id"] == "codex-direct")
    treatment = protocol["treatments"][0]
    treatment["environment"]["executable"] = str(Path(sys.executable).absolute())
    environment = EnvironmentManifest.model_validate(treatment["environment"], strict=True)
    treatment["environment_digest"] = canonical_digest(environment)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _command(manifest: Path, mode: str = "valid") -> list[str]:
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
        "--manifest",
        str(manifest),
        "--mode",
        mode,
    ]


def _collect(
    tmp_path: Path,
    mode: str = "valid",
    *,
    evaluator_mode: str = "passed",
    acceptance_evaluator: tuple[str, ...] | None = None,
) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir(exist_ok=True)
    manifest = _manifest(tmp_path / "protocols.json")
    return collect_protocol(
        manifest_path=manifest,
        protocol_id="codex-direct",
        task_path=_task(tmp_path / "task.json", evaluator_mode, acceptance_evaluator),
        repository=repository,
        output_root=tmp_path,
        timeout=10,
        network="disabled",
        arm_command=_command(manifest, mode),
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
    wrapper = str(Path("/usr/bin/env").resolve(strict=True))
    assert command["execution_argv"] == [wrapper, *command["argv"]]
    assert command["isolation"]["backend"] == "test-deny"
    assert command["isolation"]["wrapper_argv"] == [wrapper]
    assert command["isolation"]["profile_digest"] is None
    assert command["isolation"]["probe"]["exit_code"] == 0
    assert command["isolation"]["probe"]["result"]["denied"] is True
    for name in ("acceptance.json", "regression.json", "result-bundle.json"):
        assert (
            command["content_digests"][name]
            == hashlib.sha256((artifact_directory / name).read_bytes()).hexdigest()
        )
    acceptance = json.loads((artifact_directory / "acceptance.json").read_bytes())
    record = acceptance["outcomes"][0]
    assert acceptance["format"] == "fleet-productivity-check-artifact/2"
    assert record["exit_code"] == 0
    assert record["disposition"] == "passed"
    assert record["evaluator_executable"] == record["evaluator_argv"][0]
    bundle = load_result_bundle((artifact_directory / "result-bundle.json").read_bytes())
    assert (
        bundle.results[0].acceptance_outcomes[0].evidence_digest
        == hashlib.sha256((artifact_directory / "acceptance.json").read_bytes()).hexdigest()
    )


def test_collect_protocol_derives_failed_checks_from_evaluator_exit(tmp_path: Path) -> None:
    artifact_directory = _collect(tmp_path, evaluator_mode="failed").parent
    bundle = load_result_bundle((artifact_directory / "result-bundle.json").read_bytes())
    assert not bundle.results[0].accepted
    assert bundle.results[0].terminal_outcome.value == "checks_failed"
    for name in ("acceptance.json", "regression.json"):
        assert (
            json.loads((artifact_directory / name).read_bytes())["outcomes"][0]["disposition"]
            == "failed"
        )


@pytest.mark.parametrize(
    ("mode", "message"),
    (
        ("claim-artifact", "exactly one canonical draft"),
        ("extra", "exactly one canonical draft"),
        ("missing", "exactly one canonical draft"),
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


@pytest.mark.parametrize(
    ("evaluator", "message"),
    (
        ((), "must not be empty"),
        (
            _evaluator("acceptance", "passed")[:7] + _evaluator("acceptance", "passed")[9:],
            "{trial}",
        ),
    ),
)
def test_collect_protocol_rejects_invalid_evaluator_templates(
    tmp_path: Path, evaluator: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _collect(tmp_path, acceptance_evaluator=evaluator)


def test_collect_protocol_rejects_oversized_evaluator_observation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="observation exceeds size limit"):
        _collect(tmp_path, evaluator_mode="oversized")
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
    manifest = _manifest(tmp_path / "protocols.json")
    with pytest.raises(ValueError, match="network policies"):
        collect_protocol(
            manifest_path=manifest,
            protocol_id="codex-direct",
            task_path=_task(tmp_path / "task.json"),
            repository=repository,
            output_root=tmp_path,
            timeout=10,
            network="enabled",
            arm_command=_command(manifest),
        )


def test_collect_protocol_rejects_unavailable_isolation_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(_: Path) -> productivity_protocol._NetworkIsolation:
        raise ValueError("network isolation backend is unavailable")

    monkeypatch.setattr(
        productivity_protocol,
        "_establish_network_isolation",
        unavailable,
    )
    with pytest.raises(ValueError, match="isolation backend is unavailable"):
        _collect(tmp_path)
    assert not (tmp_path / "artifacts" / "codex-direct").exists()


def test_network_isolation_probe_records_exact_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = productivity_protocol._IsolationBackend(
        name="trusted-test",
        wrapper_argv=("trusted-wrapper", "--"),
        profile_digest="a" * 64,
    )

    def denied_run(*_args: object, **_kwargs: object) -> tuple[bytes, bytes, str, str, int]:
        return (
            b'{"denied":true,"errno":1}\n',
            b"",
            "2026-01-01T00:00:00.000000Z",
            "2026-01-01T00:00:00.000001Z",
            0,
        )

    monkeypatch.setattr(productivity_protocol, "_run", denied_run)
    evidence = productivity_protocol._probe_network_isolation(backend, tmp_path)
    execution_argv = evidence["execution_argv"]
    assert isinstance(execution_argv, tuple)
    assert execution_argv[:2] == ("trusted-wrapper", "--")
    assert evidence["result"] == {"denied": True, "errno": 1}


def test_network_isolation_probe_rejects_permissive_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = productivity_protocol._IsolationBackend(
        name="permissive-test",
        wrapper_argv=("trusted-wrapper", "--"),
        profile_digest=None,
    )

    def permissive_run(*_args: object, **_kwargs: object) -> tuple[bytes, bytes, str, str, int]:
        return (
            b'{"denied":false,"errno":null}\n',
            b"",
            "2026-01-01T00:00:00.000000Z",
            "2026-01-01T00:00:00.000001Z",
            97,
        )

    monkeypatch.setattr(productivity_protocol, "_run", permissive_run)
    with pytest.raises(ValueError, match="probe permitted networking"):
        productivity_protocol._probe_network_isolation(backend, tmp_path)


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
            arm_command=_command(escaped_manifest),
        )


@pytest.mark.parametrize(
    "mode",
    (
        "environment-version",
        "dependency-lock",
        "sandbox-network",
        "cache-machine",
        "prompt-context",
        "model",
        "tools",
        "budgets",
        "stopping",
        "pricing",
        "randomized-order",
    ),
)
def test_collect_protocol_rejects_self_consistent_fictional_controls(
    tmp_path: Path, mode: str
) -> None:
    with pytest.raises(ValueError, match="treatment mismatch"):
        _collect(tmp_path, mode)
    assert not (tmp_path / "artifacts" / "codex-direct").exists()


def test_collect_protocol_rejects_resolved_execution_identity_mismatch(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    manifest = _manifest(tmp_path / "protocols.json")
    with pytest.raises(ValueError, match="resolved producer executable"):
        collect_protocol(
            manifest_path=manifest,
            protocol_id="codex-direct",
            task_path=_task(tmp_path / "task.json"),
            repository=repository,
            output_root=tmp_path,
            timeout=10,
            network="disabled",
            arm_command=["/usr/bin/env", *_command(manifest)],
        )
    assert not (tmp_path / "artifacts" / "codex-direct").exists()


@pytest.mark.parametrize(
    ("digest_field", "message"),
    (
        ("environment_digest", "environment digest"),
        ("fairness_config_digest", "fairness config digest"),
    ),
)
def test_protocol_manifest_rejects_stale_predeclared_control_digest(
    tmp_path: Path, digest_field: str, message: str
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["protocols"][0]["treatments"][0][digest_field] = "f" * 64
    path = tmp_path / "protocols.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_protocol_manifest(path)
