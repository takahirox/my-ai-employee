"""Offline, fail-closed collection boundary for productivity protocols."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import ClassVar, Literal, Self

from pydantic import Field, field_validator, model_validator

from .domain.base import Digest, Identifier
from .domain.v2 import SchemaModelV2
from .productivity_evaluation import (
    ArmConfigManifest,
    ArmKind,
    CheckDisposition,
    EnvironmentManifest,
    FairnessConfigManifest,
    ResultBundle,
    TaskIdentity,
    load_result_bundle,
)
from .serialization import canonical_digest, canonical_json, loads_model

_CHECK_FILENAMES = ("acceptance.json", "regression.json", "result-bundle.json")
_PLACEHOLDERS = ("{task}", "{repository}", "{output}", "{protocol}")
_MAX_ARTIFACT_BYTES = 10_000_000


def _relative_artifact_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or value.startswith("/") or "\\" in value or ".." in path.parts:
        raise ValueError("artifact path must be a contained relative POSIX path")
    if path.as_posix() != value:
        raise ValueError("artifact path must use canonical POSIX syntax")
    return value


class ProtocolTreatment(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_protocol_treatment"
    id: Identifier
    kind: ArmKind
    adapter: Identifier
    worker: Identifier
    environment: EnvironmentManifest
    fairness_config: FairnessConfigManifest
    arm_config: ArmConfigManifest
    environment_digest: Digest
    fairness_config_digest: Digest
    disabled_components: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def _canonical_components(self) -> Self:
        if self.disabled_components != tuple(sorted(set(self.disabled_components))):
            raise ValueError("disabled components must be unique and sorted")
        if (self.kind is ArmKind.FLEET_ABLATION) != (len(self.disabled_components) == 1):
            raise ValueError("Fleet ablation treatments must disable exactly one component")
        bindings = (
            ("environment", self.environment_digest, canonical_digest(self.environment)),
            (
                "fairness config",
                self.fairness_config_digest,
                canonical_digest(self.fairness_config),
            ),
        )
        for name, supplied, actual in bindings:
            if supplied != actual:
                raise ValueError(f"{name} digest does not bind its protocol treatment manifest")
        return self


class ProtocolDefinition(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_protocol_definition"
    id: Identifier
    controlled_delta: Identifier
    network: Literal["disabled"]
    artifacts: str
    evidence: tuple[str, str, str]
    command: tuple[str, ...] = Field(min_length=1)
    treatments: tuple[ProtocolTreatment, ...] = Field(min_length=1)

    @field_validator("artifacts")
    @classmethod
    def _valid_artifact_path(cls, value: str) -> str:
        return _relative_artifact_path(value)

    @field_validator("evidence")
    @classmethod
    def _valid_evidence_paths(cls, value: tuple[str, str, str]) -> tuple[str, str, str]:
        for item in value:
            _relative_artifact_path(item)
        return value

    @model_validator(mode="after")
    def _artifact_set_is_exact(self) -> Self:
        command_path = PurePosixPath(self.artifacts)
        expected = tuple((command_path.parent / name).as_posix() for name in _CHECK_FILENAMES)
        if self.evidence != expected:
            raise ValueError("protocol must declare the exact three sibling evidence artifacts")
        if any(item.environment.network_mode != self.network for item in self.treatments):
            raise ValueError("treatment environment network policy must match the protocol")
        ids = tuple(item.id for item in self.treatments)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("protocol treatments must be unique and sorted")
        return self


class ProtocolManifest(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_protocol_manifest"
    format: Literal["fleet-productivity-protocols/2"]
    cadence: dict[str, object]
    protocols: tuple[ProtocolDefinition, ...] = Field(min_length=1)
    verification: dict[str, tuple[str, ...]]

    @model_validator(mode="after")
    def _protocols_are_canonical(self) -> Self:
        ids = tuple(item.id for item in self.protocols)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("protocols must be unique and sorted by id")
        return self


class ProtocolCheckRecord(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_protocol_check_record"
    trial_id: Identifier
    check_id: Identifier
    authority: str = Field(min_length=1, max_length=1_000)
    disposition: CheckDisposition


class ProtocolCheckArtifact(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_protocol_check_artifact"
    format: Literal["fleet-productivity-check-artifact/1"]
    family: Literal["acceptance", "regression"]
    outcomes: tuple[ProtocolCheckRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _outcomes_are_canonical(self) -> Self:
        keys = tuple((item.trial_id, item.check_id) for item in self.outcomes)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("check records must be unique and canonically ordered")
        return self


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_duplicate_keys(data: bytes) -> None:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    try:
        json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except UnicodeDecodeError as exc:
        raise ValueError("JSON input must be UTF-8") from exc


def load_protocol_manifest(path: Path) -> tuple[ProtocolManifest, bytes]:
    raw = path.read_bytes()
    _reject_duplicate_keys(raw)
    return ProtocolManifest.model_validate_json(raw, strict=True), raw


def _load_task(path: Path) -> tuple[TaskIdentity, bytes]:
    raw = path.read_bytes()
    _reject_duplicate_keys(raw)
    task = loads_model(raw, TaskIdentity)
    if raw != (canonical_json(task) + "\n").encode("utf-8"):
        raise ValueError("task input must be canonical JSON with one trailing newline")
    return task, raw


def _load_check_artifact(data: bytes, family: str) -> ProtocolCheckArtifact:
    artifact = ProtocolCheckArtifact.model_validate_json(data, strict=True)
    if artifact.family != family:
        raise ValueError(f"{family} artifact declares the wrong family")
    if data != (canonical_json(artifact) + "\n").encode("utf-8"):
        raise ValueError(f"{family} artifact is malformed or not canonical JSON")
    return artifact


def _contained(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("declared artifact path escapes the output root") from exc
    return candidate


def _resolve_argv(
    command: Sequence[str], *, task: Path, repository: Path, output: Path, protocol: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    original = tuple(command)
    if not original:
        raise ValueError("arm command must not be empty")
    for placeholder in _PLACEHOLDERS:
        exact = sum(item == placeholder for item in original)
        embedded = any(placeholder in item and item != placeholder for item in original)
        if exact != 1 or embedded:
            raise ValueError(f"arm command must contain exact placeholder once: {placeholder}")
    replacements = {
        "{task}": str(task),
        "{repository}": str(repository),
        "{output}": str(output),
        "{protocol}": protocol,
    }
    resolved = [replacements.get(item, item) for item in original]
    executable = resolved[0]
    if "/" in executable:
        executable_path = Path(executable)
        if not executable_path.is_absolute():
            executable_path = repository / executable_path
        executable_path = Path(os.path.abspath(executable_path))
        if not executable_path.is_file():
            raise ValueError("arm executable is not a regular file")
        resolved[0] = str(executable_path)
    else:
        discovered = shutil.which(executable)
        if discovered is None:
            raise ValueError(f"arm executable was not found: {executable}")
        discovered_path = Path(os.path.abspath(discovered))
        if not discovered_path.is_file():
            raise ValueError("arm executable is not a regular file")
        resolved[0] = str(discovered_path)
    return original, tuple(resolved)


def _validate_execution_contract(
    protocol: ProtocolDefinition, resolved_argv: tuple[str, ...]
) -> None:
    executable = resolved_argv[0]
    mismatched = tuple(
        item.id for item in protocol.treatments if item.environment.executable != executable
    )
    if mismatched:
        raise ValueError(
            "resolved producer executable does not match every predeclared treatment: "
            + ", ".join(mismatched)
        )


def _run(
    argv: tuple[str, ...], repository: Path, timeout: float, network: str
) -> tuple[bytes, bytes, str, str, int]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    environment = os.environ.copy()
    environment["FLEET_PRODUCTIVITY_NETWORK"] = network
    started_at = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    process = subprocess.Popen(
        argv,
        cwd=repository,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise ValueError(f"arm command timed out after {timeout} seconds") from exc
    ended_at = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return stdout, stderr, started_at, ended_at, process.returncode


def _read_outputs(stage: Path) -> dict[str, bytes]:
    actual = {item.name for item in stage.iterdir()}
    if actual != set(_CHECK_FILENAMES):
        raise ValueError("producer must create exactly acceptance, regression, and result bundle")
    outputs: dict[str, bytes] = {}
    for name in _CHECK_FILENAMES:
        path = stage / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"producer output must be a regular file: {name}")
        if path.stat().st_size > _MAX_ARTIFACT_BYTES:
            raise ValueError(f"producer output exceeds size limit: {name}")
        outputs[name] = path.read_bytes()
    return outputs


def _records(bundle: ResultBundle, family: str) -> tuple[ProtocolCheckRecord, ...]:
    records: list[ProtocolCheckRecord] = []
    for result in bundle.results:
        outcomes = (
            result.acceptance_outcomes if family == "acceptance" else result.regression_outcomes
        )
        records.extend(
            ProtocolCheckRecord(
                trial_id=result.id,
                check_id=outcome.check_id,
                authority=outcome.authority,
                disposition=outcome.disposition,
            )
            for outcome in outcomes
        )
    return tuple(sorted(records, key=lambda item: (item.trial_id, item.check_id)))


def _validate_bundle(
    protocol: ProtocolDefinition,
    task: TaskIdentity,
    bundle: ResultBundle,
    acceptance: ProtocolCheckArtifact,
    regression: ProtocolCheckArtifact,
    outputs: dict[str, bytes],
) -> None:
    if bundle.run_id != protocol.id:
        raise ValueError("result bundle run ID does not match the selected protocol")
    if any(result.task != task for result in bundle.results):
        raise ValueError("result bundle does not bind the exact supplied task")
    expected_treatments = {item.id: item for item in protocol.treatments}
    actual_arms = {result.arm.id: result.arm for result in bundle.results}
    if set(actual_arms) != set(expected_treatments):
        raise ValueError("result bundle arms do not exactly cover protocol treatments")
    for arm_id, expected in expected_treatments.items():
        arm = actual_arms[arm_id]
        if (
            arm.kind is not expected.kind
            or arm.adapter != expected.adapter
            or arm.worker != expected.worker
            or arm.environment != expected.environment
            or arm.fairness_config != expected.fairness_config
            or arm.arm_config != expected.arm_config
            or arm.environment_digest != expected.environment_digest
            or arm.fairness_config_digest != expected.fairness_config_digest
            or arm.disabled_components != expected.disabled_components
        ):
            raise ValueError(f"result bundle treatment mismatch: {arm_id}")
        if arm.environment.network_mode != protocol.network:
            raise ValueError(f"result bundle network-policy mismatch: {arm_id}")
    for family, artifact in (("acceptance", acceptance), ("regression", regression)):
        if artifact.outcomes != _records(bundle, family):
            raise ValueError(f"{family} artifact has missing, extra, or stale check records")
        evidence_digest = _digest(outputs[f"{family}.json"])
        for result in bundle.results:
            outcomes = (
                result.acceptance_outcomes if family == "acceptance" else result.regression_outcomes
            )
            if any(item.evidence_digest != evidence_digest for item in outcomes):
                raise ValueError(f"{family} evidence digest does not bind exact artifact bytes")


def collect_protocol(
    *,
    manifest_path: Path,
    protocol_id: str,
    task_path: Path,
    repository: Path,
    output_root: Path,
    timeout: float,
    network: str,
    arm_command: Sequence[str],
) -> Path:
    """Execute and atomically retain one exact, validated protocol result."""

    manifest_path = manifest_path.resolve(strict=True)
    manifest, manifest_bytes = load_protocol_manifest(manifest_path)
    selected = tuple(item for item in manifest.protocols if item.id == protocol_id)
    if len(selected) != 1:
        raise ValueError(f"unknown protocol ID: {protocol_id}")
    protocol = selected[0]
    if network != protocol.network:
        raise ValueError("caller and protocol network policies do not match")
    task_path = task_path.resolve(strict=True)
    task, task_bytes = _load_task(task_path)
    repository = repository.resolve(strict=True)
    if not repository.is_dir():
        raise ValueError("repository must be an existing directory")
    output_root = output_root.resolve(strict=True)
    if not output_root.is_dir():
        raise ValueError("output root must be an existing directory")
    command_relative = PurePosixPath(protocol.artifacts)
    final_command = _contained(output_root, protocol.artifacts)
    final_directory = final_command.parent
    if final_directory.exists():
        raise FileExistsError(f"refusing to overwrite protocol artifacts: {final_directory}")
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{protocol.id}-", dir=final_directory.parent))
    try:
        original_argv, argv = _resolve_argv(
            arm_command,
            task=task_path,
            repository=repository,
            output=stage,
            protocol=protocol.id,
        )
        _validate_execution_contract(protocol, argv)
        stdout, stderr, started_at, ended_at, exit_code = _run(argv, repository, timeout, network)
        if exit_code != 0:
            raise ValueError(f"arm command exited with {exit_code}")
        outputs = _read_outputs(stage)
        acceptance = _load_check_artifact(outputs["acceptance.json"], "acceptance")
        regression = _load_check_artifact(outputs["regression.json"], "regression")
        bundle = load_result_bundle(outputs["result-bundle.json"])
        _validate_bundle(protocol, task, bundle, acceptance, regression, outputs)
        content_digests = {name: _digest(value) for name, value in sorted(outputs.items())}
        command_record = {
            "argv": argv,
            "arm_command": original_argv,
            "content_digests": content_digests,
            "cwd": str(repository),
            "ended_at": ended_at,
            "exit_code": exit_code,
            "format": "fleet-productivity-command/1",
            "manifest_digest": _digest(manifest_bytes),
            "manifest_path": str(manifest_path),
            "network": network,
            "protocol": protocol,
            "protocol_digest": canonical_digest(protocol),
            "result_bundle_content_digest": bundle.bundle_digest,
            "started_at": started_at,
            "stderr_digest": _digest(stderr),
            "stdout_digest": _digest(stdout),
            "task": task,
            "task_digest": _digest(task_bytes),
            "task_path": str(task_path),
            "timeout_seconds": timeout,
        }
        (stage / command_relative.name).write_bytes(
            (canonical_json(command_record) + "\n").encode("utf-8")
        )
        if final_directory.exists():
            raise FileExistsError(f"refusing to overwrite protocol artifacts: {final_directory}")
        os.rename(stage, final_directory)
        return final_command
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ai_employee.productivity_protocol")
    parser.add_argument("--manifest", default="examples/productivity/protocols.json")
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output-root", default=".")
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--network", required=True)
    parser.add_argument("arm_command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    arm_command = tuple(args.arm_command)
    if arm_command[:1] == ("--",):
        arm_command = arm_command[1:]
    if not arm_command or arm_command[0] == "--":
        parser.error("an arm command is required after --")
    try:
        result = collect_protocol(
            manifest_path=Path(args.manifest),
            protocol_id=args.protocol,
            task_path=Path(args.task),
            repository=Path(args.repository),
            output_root=Path(args.output_root),
            timeout=args.timeout,
            network=args.network,
            arm_command=arm_command,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(result)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the importable main
    raise SystemExit(main())
