"""Offline, fail-closed collection boundary for productivity protocols."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import platform
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import ClassVar, Literal, Self, cast

from pydantic import Field, field_validator, model_validator

from .domain.base import Digest, Identifier
from .domain.v2 import SchemaModelV2
from .productivity_evaluation import (
    ArmConfigManifest,
    ArmKind,
    CheckDisposition,
    CheckOutcome,
    EnvironmentManifest,
    FailureClassification,
    FairnessConfigManifest,
    ResultBundle,
    TaskIdentity,
    TerminalOutcome,
    TrialResult,
    dump_result_bundle,
    load_result_bundle,
)
from .serialization import canonical_digest, canonical_json, loads_model

_CHECK_FILENAMES = ("acceptance.json", "regression.json", "result-bundle.json")
_PRODUCER_PLACEHOLDERS = ("{task}", "{repository}", "{output}", "{protocol}")
_EVALUATOR_PLACEHOLDERS = ("{task}", "{repository}", "{trial}", "{protocol}")
_MAX_ARTIFACT_BYTES = 10_000_000
_MAX_OBSERVATION_BYTES = 1_000_000

_NETWORK_PROBE_SCRIPT = """\
import socket
import sys

try:
    connection = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=1)
except OSError as error:
    print(f'{{"denied":true,"errno":{error.errno}}}')
    raise SystemExit(0)
else:
    connection.close()
    print('{"denied":false,"errno":null}')
    raise SystemExit(97)
"""
_NETWORK_PROBE_TIMEOUT_SECONDS = 5.0


class _SockFilter(ctypes.Structure):
    _fields_ = (
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint),
    )


class _SockFprog(ctypes.Structure):
    _fields_ = (("length", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter)))


@dataclass(frozen=True)
class _IsolationBackend:
    name: str
    wrapper_argv: tuple[str, ...]
    profile_digest: str | None


@dataclass(frozen=True)
class _NetworkIsolation:
    backend: _IsolationBackend
    probe: Mapping[str, object]


@dataclass(frozen=True)
class _PinnedFile:
    source: Path
    descriptor: int
    identity: tuple[int, int, int, int, int]
    content_digest: Digest

    @property
    def execution_path(self) -> str:
        return f"/proc/self/fd/{self.descriptor}"


@dataclass(frozen=True)
class _PinnedEvaluator:
    inputs: tuple[tuple[int, _PinnedFile], ...]

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return tuple(item.descriptor for _, item in self.inputs)

    @property
    def content_digests(self) -> tuple[Digest, ...]:
        return tuple(item.content_digest for _, item in self.inputs)


def _wrapped_argv(backend: _IsolationBackend, payload_argv: tuple[str, ...]) -> tuple[str, ...]:
    return (*backend.wrapper_argv, *payload_argv)


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
    evaluator_argv: tuple[str, ...] = Field(min_length=1)
    evaluator_executable: str = Field(min_length=1, max_length=4_096)
    evaluator_input_digests: tuple[Digest, ...] = Field(min_length=1)
    exit_code: int
    observation_digest: Digest
    disposition: CheckDisposition


class ProtocolCheckArtifact(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_protocol_check_artifact"
    format: Literal["fleet-productivity-check-artifact/2"]
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


def _load_task_bytes(raw: bytes) -> tuple[TaskIdentity, bytes]:
    _reject_duplicate_keys(raw)
    task = loads_model(raw, TaskIdentity)
    if raw != (canonical_json(task) + "\n").encode("utf-8"):
        raise ValueError("task input must be canonical JSON with one trailing newline")
    return task, raw


def _load_task(path: Path) -> tuple[TaskIdentity, bytes]:
    return _load_task_bytes(path.read_bytes())


def _pin_regular_file(path: Path, *, label: str) -> tuple[_PinnedFile, bytes]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    source_descriptor = os.open(path, flags)
    try:
        status = os.fstat(source_descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise ValueError(f"{label} must be a regular file")
        data = _read_descriptor(source_descriptor, label=label)
        identity = _file_identity(status)
    finally:
        os.close(source_descriptor)
    memfd_create = getattr(os, "memfd_create", None)
    if memfd_create is None:
        raise ValueError("secure protocol collection requires sealed memfd support")
    descriptor = memfd_create(label, 0x0001 | 0x0002)
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(descriptor, 1033, 0x000F)
    except BaseException:
        os.close(descriptor)
        raise
    return (
        _PinnedFile(
            source=path,
            descriptor=descriptor,
            identity=identity,
            content_digest=_digest(data),
        ),
        data,
    )


def _verify_pinned_source(pinned: _PinnedFile, *, label: str) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(pinned.source, flags)
    except OSError as exc:
        raise ValueError(f"{label} identity changed during producer execution") from exc
    try:
        status = os.fstat(descriptor)
        data = _read_descriptor(descriptor, label=label)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(status.st_mode)
        or _file_identity(status) != pinned.identity
        or _digest(data) != pinned.content_digest
    ):
        raise ValueError(f"{label} identity changed during producer execution")


def _file_identity(status: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _read_descriptor(descriptor: int, *, label: str) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    data = bytearray()
    while len(data) <= _MAX_ARTIFACT_BYTES:
        chunk = os.read(descriptor, min(64 * 1024, _MAX_ARTIFACT_BYTES + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    if len(data) > _MAX_ARTIFACT_BYTES:
        raise ValueError(f"{label} exceeds configured byte limit")
    return bytes(data)


def _load_check_artifact(data: bytes, family: str) -> ProtocolCheckArtifact:
    artifact = ProtocolCheckArtifact.model_validate_json(data, strict=True)
    if artifact.family != family:
        raise ValueError(f"{family} artifact declares the wrong family")
    if data != (canonical_json(artifact) + "\n").encode("utf-8"):
        raise ValueError(f"{family} artifact is malformed or not canonical JSON")
    return artifact


def _validate_argv_template(
    command: Sequence[str], placeholders: Sequence[str], label: str
) -> tuple[str, ...]:
    original = tuple(command)
    if not original:
        raise ValueError(f"{label} command must not be empty")
    if any(not item or "\x00" in item for item in original):
        raise ValueError(f"{label} command arguments must be nonempty and contain no NUL")
    for placeholder in placeholders:
        exact = sum(item == placeholder for item in original)
        embedded = any(placeholder in item and item != placeholder for item in original)
        if exact != 1 or embedded:
            raise ValueError(f"{label} command must contain exact placeholder once: {placeholder}")
    known = set(placeholders)
    if any("{" in item or "}" in item for item in original if item not in known):
        raise ValueError(f"{label} command contains an unsupported placeholder")
    return original


def _resolve_argv(
    command: Sequence[str],
    *,
    replacements: Mapping[str, str],
    placeholders: Sequence[str],
    repository: Path,
    label: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    original = _validate_argv_template(command, placeholders, label)
    resolved = [replacements.get(item, item) for item in original]
    executable = resolved[0]
    if "/" in executable:
        executable_path = Path(executable)
        if not executable_path.is_absolute():
            executable_path = repository / executable_path
        executable_path = Path(os.path.abspath(executable_path))
        if not executable_path.is_file():
            raise ValueError(f"{label} executable is not a regular file")
        resolved[0] = str(executable_path)
    else:
        discovered = shutil.which(executable)
        if discovered is None:
            raise ValueError(f"{label} executable was not found: {executable}")
        discovered_path = Path(os.path.abspath(discovered))
        if not discovered_path.is_file():
            raise ValueError(f"{label} executable is not a regular file")
        resolved[0] = str(discovered_path)
    return original, tuple(resolved)


def _pin_evaluator_inputs(
    task: TaskIdentity,
    repository: Path,
    descriptors: contextlib.ExitStack,
) -> tuple[dict[tuple[str, str], _PinnedEvaluator], tuple[_PinnedFile, ...]]:
    pinned_by_path: dict[Path, _PinnedFile] = {}
    evaluators: dict[tuple[str, str], _PinnedEvaluator] = {}
    for family, checks in (
        ("acceptance", task.acceptance_criteria),
        ("regression", task.regression_checks),
    ):
        for check in checks:
            original, resolved = _resolve_argv(
                check.evaluator_argv,
                replacements={
                    "{task}": "task-authority",
                    "{repository}": str(repository),
                    "{trial}": "trial-authority",
                    "{protocol}": "protocol-authority",
                },
                placeholders=_EVALUATOR_PLACEHOLDERS,
                repository=repository,
                label=f"{family} evaluator {check.id}",
            )
            if "-m" in original[1:]:
                raise ValueError(f"{family} evaluator {check.id} may not load an unpinned module")
            indexed: list[tuple[int, _PinnedFile]] = []
            for index, item in enumerate(original):
                candidate: Path | None = None
                if index == 0:
                    candidate = Path(resolved[0])
                elif item not in _EVALUATOR_PLACEHOLDERS:
                    literal = Path(item)
                    if literal.is_absolute() or "/" in item:
                        candidate = (
                            literal
                            if literal.is_absolute()
                            else Path(os.path.abspath(repository / literal))
                        )
                        if not candidate.is_file():
                            raise ValueError(
                                f"{family} evaluator {check.id} authority input is missing"
                            )
                    else:
                        repository_candidate = repository / literal
                        if repository_candidate.is_file():
                            candidate = Path(os.path.abspath(repository_candidate))
                if candidate is None:
                    continue
                pinned = pinned_by_path.get(candidate)
                if pinned is None:
                    pinned, _ = _pin_regular_file(
                        candidate,
                        label=f"{family} evaluator {check.id} authority input",
                    )
                    pinned_by_path[candidate] = pinned
                    descriptors.callback(os.close, pinned.descriptor)
                indexed.append((index, pinned))
            evaluators[(family, check.id)] = _PinnedEvaluator(inputs=tuple(indexed))
    return evaluators, tuple(pinned_by_path.values())


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


def _resolve_isolation_backend() -> _IsolationBackend:
    if not sys.platform.startswith("linux"):
        raise ValueError("secure protocol collection requires Linux containment primitives")
    discovered = shutil.which("unshare")
    if discovered is None:
        raise ValueError("Linux network namespace backend is unavailable")
    executable = Path(discovered).resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("Linux network namespace backend is unavailable")
    return _IsolationBackend(
        name="linux-unshare-user-net",
        wrapper_argv=(
            str(executable),
            "--user",
            "--map-root-user",
            "--net",
            "--",
        ),
        profile_digest=None,
    )


def _require_secure_runtime() -> None:
    if (
        not sys.platform.startswith("linux")
        or not Path("/proc/self/stat").is_file()
        or any(not hasattr(os, name) for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"))
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        raise ValueError(
            "secure protocol collection requires Linux procfs and no-follow dir-fd primitives"
        )
    library = ctypes.CDLL(None)
    if getattr(library, "prctl", None) is None or getattr(library, "renameat2", None) is None:
        raise ValueError("secure protocol collection requires prctl and renameat2")


def _process_snapshot() -> dict[int, tuple[int, str]]:
    result: dict[int, tuple[int, str]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdecimal():
            continue
        try:
            raw = Path("/proc", entry, "stat").read_text(encoding="utf-8")
            fields = raw[raw.rindex(")") + 2 :].split()
            result[int(entry)] = (int(fields[1]), fields[19])
        except (FileNotFoundError, ProcessLookupError):
            pass
    return result


def _set_subreaper(enabled: bool) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    if library.prctl(36, int(enabled), 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise ValueError(f"cannot configure child-subreaper containment: {os.strerror(error)}")


def _descendants(
    leader: int,
    baseline: Mapping[int, tuple[int, str]],
    observed: dict[int, tuple[int, str]],
) -> tuple[int, ...]:
    current = _process_snapshot()
    if leader in current:
        observed.setdefault(leader, current[leader])
    changed = True
    while changed:
        changed = False
        parents = {leader, *observed}
        for pid, identity in current.items():
            if identity[0] in parents and observed.get(pid) != identity:
                observed[pid] = identity
                changed = True
    for pid, identity in current.items():
        if identity[0] == os.getpid() and baseline.get(pid) != identity:
            observed[pid] = identity
    return tuple(pid for pid, identity in observed.items() if current.get(pid) == identity)


def _terminate_tree(
    process: subprocess.Popen[bytes],
    baseline: Mapping[int, tuple[int, str]],
    observed: dict[int, tuple[int, str]],
) -> None:
    for sig, grace in ((signal.SIGTERM, 0.25), (signal.SIGKILL, 2.0)):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, sig)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            alive = _descendants(process.pid, baseline, observed)
            for pid in alive:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, sig)
                if pid != process.pid:
                    with contextlib.suppress(ChildProcessError):
                        os.waitpid(pid, os.WNOHANG)
            if process.poll() is not None and not alive:
                return
            time.sleep(0.01)
    raise ValueError("protocol process-tree cleanup could not be confirmed")


def _install_socket_seccomp() -> None:
    syscall_numbers = {
        "x86_64": (41, 42, 53),
        "amd64": (41, 42, 53),
        "aarch64": (198, 203, 199),
        "arm64": (198, 203, 199),
    }.get(platform.machine().lower())
    if syscall_numbers is None:
        raise OSError(errno.ENOTSUP, "unsupported architecture for socket seccomp")
    instructions = [_SockFilter(0x20, 0, 0, 0)]
    for number in syscall_numbers:
        instructions.extend(
            (
                _SockFilter(0x15, 0, 1, number),
                _SockFilter(0x06, 0, 0, 0x00050000 | errno.EPERM),
            )
        )
    instructions.append(_SockFilter(0x06, 0, 0, 0x7FFF0000))
    filters = (_SockFilter * len(instructions))(*instructions)
    program = _SockFprog(len(instructions), filters)
    library = ctypes.CDLL(None, use_errno=True)
    if library.prctl(38, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if library.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _run(
    argv: tuple[str, ...],
    repository: Path,
    timeout: float,
    network: str,
    *,
    isolation: _IsolationBackend,
    observation_limit: int | None = None,
    pass_fds: tuple[int, ...] = (),
) -> tuple[bytes, bytes, str, str, int]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    _require_secure_runtime()
    environment = {
        "FLEET_PRODUCTIVITY_NETWORK": network,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    if "PYTHONPATH" in os.environ:
        environment["PYTHONPATH"] = os.environ["PYTHONPATH"]
    started_at = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    baseline = _process_snapshot()
    _set_subreaper(True)
    process: subprocess.Popen[bytes] | None = None
    observed: dict[int, tuple[int, str]] = {}
    cleaned = False
    try:
        process = subprocess.Popen(
            _wrapped_argv(isolation, argv),
            cwd=repository,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=pass_fds,
            preexec_fn=_install_socket_seccomp,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        captured: tuple[bytes, bytes] | None = None
        expired = False
        while captured is None:
            _descendants(process.pid, baseline, observed)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                expired = True
                break
            try:
                captured = process.communicate(timeout=min(0.02, remaining))
            except subprocess.TimeoutExpired:
                if process.poll() is not None:
                    break
        exit_code = process.poll()
        _terminate_tree(process, baseline, observed)
        cleaned = True
        if captured is None:
            captured = process.communicate(timeout=2)
        if expired:
            raise ValueError(f"arm command timed out after {timeout} seconds")
        if exit_code is None:
            raise ValueError("protocol process exited without a stable status")
        stdout, stderr = captured
    finally:
        if process is not None and not cleaned:
            _terminate_tree(process, baseline, observed)
        _set_subreaper(False)
    if observation_limit is not None and len(stdout) + len(stderr) > observation_limit:
        raise ValueError("evaluator observation exceeds size limit")
    ended_at = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return stdout, stderr, started_at, ended_at, exit_code


def _probe_network_isolation(backend: _IsolationBackend, repository: Path) -> Mapping[str, object]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        payload_argv = (
            str(Path(sys.executable).resolve(strict=True)),
            "-c",
            _NETWORK_PROBE_SCRIPT,
            str(port),
        )
        stdout, stderr, started_at, ended_at, exit_code = _run(
            payload_argv,
            repository,
            _NETWORK_PROBE_TIMEOUT_SECONDS,
            "disabled",
            isolation=backend,
            observation_limit=4_096,
        )
    finally:
        listener.close()
    if exit_code == 97:
        raise ValueError("network isolation probe permitted networking")
    if exit_code != 0:
        raise ValueError(
            f"network isolation backend or probe is unavailable (exit code {exit_code})"
        )
    try:
        parsed = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("network isolation probe returned invalid evidence") from exc
    if not isinstance(parsed, dict):
        raise ValueError("network isolation probe returned invalid evidence")
    evidence = cast(dict[str, object], parsed)
    if set(evidence) != {"denied", "errno"}:
        raise ValueError("network isolation probe returned invalid evidence")
    denied = evidence["denied"]
    error_number = evidence["errno"]
    if denied is not True or not isinstance(error_number, int) or isinstance(error_number, bool):
        raise ValueError("network isolation probe did not prove socket denial")
    return {
        "ended_at": ended_at,
        "execution_argv": _wrapped_argv(backend, payload_argv),
        "exit_code": exit_code,
        "payload_argv": payload_argv,
        "result": evidence,
        "started_at": started_at,
        "stderr_digest": _digest(stderr),
        "stdout_digest": _digest(stdout),
    }


def _establish_network_isolation(repository: Path) -> _NetworkIsolation:
    backend = _resolve_isolation_backend()
    return _NetworkIsolation(
        backend=backend,
        probe=_probe_network_isolation(backend, repository),
    )


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_directory(path: Path | str, parent: int | None = None) -> int:
    relative = os.fspath(path)
    if parent is not None and PurePosixPath(relative).parts != (relative,):
        raise ValueError("protocol directory traversal is not allowed")
    try:
        descriptor = os.open(path, _directory_flags(), dir_fd=parent)
    except OSError as exc:
        raise ValueError("protocol directory was replaced or is unsafe") from exc
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode):
        os.close(descriptor)
        raise ValueError("protocol directory is not a directory")
    if parent is not None:
        try:
            named_status = os.stat(path, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            os.close(descriptor)
            raise ValueError("protocol directory was replaced or is unsafe") from exc
        if not stat.S_ISDIR(named_status.st_mode) or (named_status.st_dev, named_status.st_ino) != (
            status.st_dev,
            status.st_ino,
        ):
            os.close(descriptor)
            raise ValueError("protocol directory identity changed while opening")
    return descriptor


def _identity(descriptor: int) -> tuple[int, int]:
    status = os.fstat(descriptor)
    return status.st_dev, status.st_ino


def _open_artifact_parent(
    root: int, components: tuple[str, ...]
) -> tuple[int, tuple[tuple[int, int], ...]]:
    identities: list[tuple[int, int]] = []
    with contextlib.ExitStack() as opened:
        current = os.dup(root)
        opened.callback(os.close, current)
        for name in components:
            created = False
            try:
                os.mkdir(name, mode=0o700, dir_fd=current)
                created = True
            except FileExistsError:
                pass
            child = _open_directory(name, current)
            opened.callback(os.close, child)
            if created:
                os.fsync(child)
                os.fsync(current)
            identities.append(_identity(child))
            current = child
        return os.dup(current), tuple(identities)


def _require_name_absent(parent: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError("protocol destination could not be inspected safely") from exc
    raise FileExistsError(f"refusing to overwrite protocol artifacts: {name}")


def _create_staging_directory(parent: int, protocol_id: str) -> tuple[str, int]:
    for _ in range(128):
        name = f".{protocol_id}-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            continue
        try:
            return name, _open_directory(name, parent)
        except BaseException:
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=parent)
            raise
    raise FileExistsError("could not allocate a unique protocol staging directory")


def _verify_published_path(
    *,
    root_path: Path,
    root_identity: tuple[int, int],
    parent_components: tuple[str, ...],
    parent_identities: tuple[tuple[int, int], ...],
    final_name: str,
    final_identity: tuple[int, int],
) -> None:
    with contextlib.ExitStack() as opened:
        current = _open_directory(root_path)
        opened.callback(os.close, current)
        if _identity(current) != root_identity:
            raise ValueError("published protocol output root identity changed")
        for name, expected_identity in zip(parent_components, parent_identities, strict=True):
            current = _open_directory(name, current)
            opened.callback(os.close, current)
            if _identity(current) != expected_identity:
                raise ValueError("published protocol ancestry changed")
        final = _open_directory(final_name, current)
        opened.callback(os.close, final)
        if _identity(final) != final_identity:
            raise ValueError("published protocol directory identity changed")


def _discard_staging_directory(
    parent: int, name: str, descriptor: int, expected_identity: tuple[int, int]
) -> None:
    os.fchmod(descriptor, 0o700)
    for artifact_name in os.listdir(descriptor):
        os.unlink(artifact_name, dir_fd=descriptor)
    try:
        named_stage = _open_directory(name, parent)
    except ValueError:
        return
    try:
        if _identity(named_stage) == expected_identity:
            os.rmdir(name, dir_fd=parent)
    finally:
        os.close(named_stage)


def _read_regular(directory: int, name: str) -> bytes:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    except OSError as exc:
        raise ValueError(f"artifact is missing, replaced, or unsafe: {name}") from exc
    try:
        before = os.fstat(descriptor)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError(f"artifact must be a single regular file: {name}")
        data = bytearray()
        while len(data) <= _MAX_ARTIFACT_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_ARTIFACT_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if len(data) > _MAX_ARTIFACT_BYTES:
            raise ValueError(f"artifact exceeds size limit: {name}")
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError(f"artifact changed while being read: {name}")
        return bytes(data)
    finally:
        os.close(descriptor)


def _write_regular(directory: int, name: str, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(name, flags, 0o400, dir_fd=directory)
    try:
        remaining = memoryview(data)
        while remaining:
            remaining = remaining[os.write(descriptor, remaining) :]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
    finally:
        os.close(descriptor)


def _read_draft(stage: int) -> bytes:
    actual = set(os.listdir(stage))
    if actual != {"result-bundle.json"}:
        raise ValueError("producer must create exactly one canonical draft result-bundle.json")
    return _read_regular(stage, "result-bundle.json")


def _rename_noreplace(parent: int, source: str, destination: str) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function = library.renameat2
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    if function(parent, os.fsencode(source), parent, os.fsencode(destination), 1) == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(f"refusing to overwrite protocol artifacts: {destination}")
    raise OSError(error, os.strerror(error))


def _rollback_publication(
    parent: int,
    final_name: str,
    stage_name: str,
    expected_identity: tuple[int, int],
) -> None:
    try:
        _rename_noreplace(parent, final_name, stage_name)
        named_stage = _open_directory(stage_name, parent)
        try:
            if _identity(named_stage) != expected_identity:
                raise RuntimeError("restored staging directory identity changed")
        finally:
            os.close(named_stage)
        try:
            os.stat(final_name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("published destination still exists after rollback")
        os.fsync(parent)
    except BaseException as exc:
        raise RuntimeError("published protocol rollback could not be proven") from exc


def _validate_evaluator_templates(task: TaskIdentity) -> None:
    for family, checks in (
        ("acceptance", task.acceptance_criteria),
        ("regression", task.regression_checks),
    ):
        for check in checks:
            _validate_argv_template(
                check.evaluator_argv,
                _EVALUATOR_PLACEHOLDERS,
                f"{family} evaluator {check.id}",
            )


def _validate_draft_bundle(
    protocol: ProtocolDefinition,
    task: TaskIdentity,
    bundle: ResultBundle,
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


def _evaluate_checks(
    *,
    task_path: Path,
    task_descriptor: int,
    task: TaskIdentity,
    repository: Path,
    protocol: ProtocolDefinition,
    draft: ResultBundle,
    timeout: float,
    network: str,
    isolation: _IsolationBackend,
    evaluator_pins: Mapping[tuple[str, str], _PinnedEvaluator],
    collection_started: float,
) -> tuple[ResultBundle, dict[str, bytes]]:
    records: dict[str, list[ProtocolCheckRecord]] = {"acceptance": [], "regression": []}
    dispositions: dict[tuple[str, str, str], CheckDisposition] = {}
    for result in draft.results:
        for family, checks in (
            ("acceptance", task.acceptance_criteria),
            ("regression", task.regression_checks),
        ):
            for check in checks:
                _, argv = _resolve_argv(
                    check.evaluator_argv,
                    replacements={
                        "{task}": str(task_path),
                        "{repository}": str(repository),
                        "{trial}": result.id,
                        "{protocol}": protocol.id,
                    },
                    placeholders=_EVALUATOR_PLACEHOLDERS,
                    repository=repository,
                    label=f"{family} evaluator {check.id}",
                )
                pinned = evaluator_pins[(family, check.id)]
                execution_argv = list(argv)
                for index, authority_input in pinned.inputs:
                    execution_argv[index] = authority_input.execution_path
                executed = tuple(execution_argv)
                stdout, stderr, _, _, exit_code = _run(
                    executed,
                    repository,
                    timeout,
                    network,
                    isolation=isolation,
                    observation_limit=_MAX_OBSERVATION_BYTES,
                    pass_fds=(task_descriptor, *pinned.pass_fds),
                )
                disposition = CheckDisposition.PASSED if exit_code == 0 else CheckDisposition.FAILED
                dispositions[(result.id, family, check.id)] = disposition
                records[family].append(
                    ProtocolCheckRecord(
                        trial_id=result.id,
                        check_id=check.id,
                        authority=check.authority,
                        evaluator_argv=executed,
                        evaluator_executable=executed[0],
                        evaluator_input_digests=pinned.content_digests,
                        exit_code=exit_code,
                        observation_digest=_digest(stdout + stderr),
                        disposition=disposition,
                    )
                )
    artifacts = {
        family: ProtocolCheckArtifact(
            format="fleet-productivity-check-artifact/2",
            family=cast(Literal["acceptance", "regression"], family),
            outcomes=tuple(sorted(items, key=lambda item: (item.trial_id, item.check_id))),
        )
        for family, items in records.items()
    }
    outputs = {
        f"{family}.json": (canonical_json(artifact) + "\n").encode("utf-8")
        for family, artifact in artifacts.items()
    }
    digests = {family: _digest(outputs[f"{family}.json"]) for family in artifacts}
    observed_wall = max(0.0, time.monotonic() - collection_started)
    final_results: list[TrialResult] = []
    for result in draft.results:
        updates: dict[str, object] = {}
        all_passed = True
        for family, checks in (
            ("acceptance", task.acceptance_criteria),
            ("regression", task.regression_checks),
        ):
            outcomes = tuple(
                CheckOutcome(
                    check_id=check.id,
                    authority=check.authority,
                    disposition=dispositions[(result.id, family, check.id)],
                    evidence_digest=digests[family],
                )
                for check in checks
            )
            all_passed = all_passed and all(
                item.disposition is CheckDisposition.PASSED for item in outcomes
            )
            updates[f"{family}_outcomes"] = outcomes
        updates["metrics"] = result.metrics.model_copy(
            update={
                "wall_seconds": observed_wall,
                "time_to_accepted_seconds": observed_wall if all_passed else None,
                "compute_seconds": observed_wall,
                "critical_path_seconds": observed_wall,
            }
        )
        if not all_passed:
            updates.update(
                terminal_outcome=TerminalOutcome.CHECKS_FAILED,
                failure_classification=FailureClassification.ASSERTION,
                process_exit_code=0,
            )
        values = result.model_dump(mode="python")
        values.update(updates)
        final_results.append(TrialResult.model_validate(values))
    bundle_values = draft.model_dump(mode="python", exclude={"bundle_digest"})
    bundle_values["results"] = tuple(final_results)
    bundle = ResultBundle.model_validate(bundle_values)
    outputs["result-bundle.json"] = dump_result_bundle(bundle)
    return bundle, outputs


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

    _require_secure_runtime()
    manifest_path = manifest_path.resolve(strict=True)
    manifest, manifest_bytes = load_protocol_manifest(manifest_path)
    selected = tuple(item for item in manifest.protocols if item.id == protocol_id)
    if len(selected) != 1:
        raise ValueError(f"unknown protocol ID: {protocol_id}")
    protocol = selected[0]
    if network != protocol.network:
        raise ValueError("caller and protocol network policies do not match")
    wall_budget = min(item.fairness_config.budgets.wall_seconds for item in protocol.treatments)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > wall_budget:
        raise ValueError(
            "collector timeout must be positive and within every treatment wall budget"
        )
    task_path = task_path.resolve(strict=True)
    repository = repository.resolve(strict=True)
    if not repository.is_dir():
        raise ValueError("repository must be an existing directory")
    output_root = output_root.resolve(strict=True)
    command_relative = PurePosixPath(protocol.artifacts)
    directory_parts = command_relative.parent.parts
    if not directory_parts:
        raise FileExistsError(f"refusing to overwrite protocol artifacts: {output_root}")
    parent_components = directory_parts[:-1]
    final_name = directory_parts[-1]
    final_command = output_root.joinpath(*command_relative.parts)
    with contextlib.ExitStack() as descriptors:
        pinned_task, task_bytes = _pin_regular_file(task_path, label="task input")
        descriptors.callback(os.close, pinned_task.descriptor)
        task, _ = _load_task_bytes(task_bytes)
        _validate_evaluator_templates(task)
        evaluator_pins, evaluator_sources = _pin_evaluator_inputs(task, repository, descriptors)
        root_descriptor = _open_directory(output_root)
        descriptors.callback(os.close, root_descriptor)
        root_identity = _identity(root_descriptor)
        isolation = _establish_network_isolation(repository)
        parent_descriptor, parent_identities = _open_artifact_parent(
            root_descriptor, parent_components
        )
        descriptors.callback(os.close, parent_descriptor)
        _require_name_absent(parent_descriptor, final_name)
        stage_name, stage_descriptor = _create_staging_directory(parent_descriptor, protocol.id)
        descriptors.callback(os.close, stage_descriptor)
        stage_identity = _identity(stage_descriptor)
        stage = output_root.joinpath(*parent_components, stage_name)
        published = False
        try:
            original_argv, argv = _resolve_argv(
                arm_command,
                replacements={
                    "{task}": str(task_path),
                    "{repository}": str(repository),
                    "{output}": str(stage),
                    "{protocol}": protocol.id,
                },
                placeholders=_PRODUCER_PLACEHOLDERS,
                repository=repository,
                label="arm",
            )
            _validate_execution_contract(protocol, argv)
            collection_started = time.monotonic()
            stdout, stderr, started_at, ended_at, exit_code = _run(
                argv,
                repository,
                timeout,
                network,
                isolation=isolation.backend,
            )
            _verify_pinned_source(pinned_task, label="task input")
            for source in evaluator_sources:
                _verify_pinned_source(source, label="evaluator authority input")
            if exit_code != 0:
                raise ValueError(f"arm command exited with {exit_code}")
            draft_bytes = _read_draft(stage_descriptor)
            draft = load_result_bundle(draft_bytes)
            _validate_draft_bundle(protocol, task, draft)
            bundle, outputs = _evaluate_checks(
                task_path=Path(pinned_task.execution_path),
                task_descriptor=pinned_task.descriptor,
                task=task,
                repository=repository,
                protocol=protocol,
                draft=draft,
                timeout=timeout,
                network=network,
                isolation=isolation.backend,
                evaluator_pins=evaluator_pins,
                collection_started=collection_started,
            )
            for source in evaluator_sources:
                _verify_pinned_source(source, label="evaluator authority input")
            if set(os.listdir(stage_descriptor)) != {"result-bundle.json"}:
                raise ValueError("staging artifacts changed after validation")
            os.unlink("result-bundle.json", dir_fd=stage_descriptor)
            for name, data in sorted(outputs.items()):
                _write_regular(stage_descriptor, name, data)
            content_digests = {name: _digest(value) for name, value in sorted(outputs.items())}
            command_record = {
                "argv": argv,
                "arm_command": original_argv,
                "content_digests": content_digests,
                "cwd": str(repository),
                "ended_at": ended_at,
                "execution_argv": _wrapped_argv(isolation.backend, argv),
                "exit_code": exit_code,
                "format": "fleet-productivity-command/1",
                "isolation": {
                    "backend": isolation.backend.name,
                    "probe": isolation.probe,
                    "profile_digest": isolation.backend.profile_digest,
                    "wrapper_argv": isolation.backend.wrapper_argv,
                },
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
            command_bytes = (canonical_json(command_record) + "\n").encode("utf-8")
            _write_regular(stage_descriptor, command_relative.name, command_bytes)
            expected = {**outputs, command_relative.name: command_bytes}
            if set(os.listdir(stage_descriptor)) != set(expected):
                raise ValueError("staging artifact set changed before publication")
            for name, data in sorted(expected.items()):
                if _read_regular(stage_descriptor, name) != data:
                    raise ValueError(f"artifact changed before publication: {name}")
            os.fchmod(stage_descriptor, 0o500)
            os.fsync(stage_descriptor)
            named_stage = _open_directory(stage_name, parent_descriptor)
            try:
                if _identity(named_stage) != stage_identity:
                    raise ValueError("protocol staging directory identity changed")
            finally:
                os.close(named_stage)
            _rename_noreplace(parent_descriptor, stage_name, final_name)
            published = True
            final_descriptor = _open_directory(final_name, parent_descriptor)
            try:
                if _identity(final_descriptor) != stage_identity:
                    raise ValueError("published protocol directory identity changed")
                if set(os.listdir(final_descriptor)) != set(expected):
                    raise ValueError("published artifact set changed")
                for name, data in sorted(expected.items()):
                    if _read_regular(final_descriptor, name) != data:
                        raise ValueError(f"published artifact changed: {name}")
                os.fsync(final_descriptor)
            finally:
                os.close(final_descriptor)
            os.fsync(parent_descriptor)
            _verify_published_path(
                root_path=output_root,
                root_identity=root_identity,
                parent_components=parent_components,
                parent_identities=parent_identities,
                final_name=final_name,
                final_identity=stage_identity,
            )
            return final_command
        except BaseException:
            if published:
                _rollback_publication(
                    parent_descriptor,
                    final_name,
                    stage_name,
                    stage_identity,
                )
                published = False
            raise
        finally:
            if not published:
                _discard_staging_directory(
                    parent_descriptor,
                    stage_name,
                    stage_descriptor,
                    stage_identity,
                )


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
