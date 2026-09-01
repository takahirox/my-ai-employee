from __future__ import annotations

import io
import os
import selectors
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import BinaryIO, Literal

from ai_employee.domain.base import freeze_json
from ai_employee.domain.services_v2 import ArtifactStore, Cancellation
from ai_employee.domain.v2 import (
    ArtifactDescriptor,
    ArtifactPutRequest,
    DecisionOutcome,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
    StableFailure,
    StableFailureCode,
)

from ._common import identifier, now


class _ProcessGroupCleanupError(RuntimeError):
    pass


_SECRET_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "API_KEY",
    "AUTH",
    "ACCESS_KEY",
    "PRIVATE_KEY",
)


class LocalProcessExecutor:
    """Argv-only subprocess execution contained to explicitly authorized roots."""

    def __init__(
        self,
        roots: Sequence[str | Path],
        artifacts: ArtifactStore,
        *,
        executable_paths: Sequence[str | Path] = ("/usr/bin", "/bin"),
        inherited_environment: Mapping[str, str] | None = None,
        secret_resolver: Callable[[str], str] | None = None,
        stdin_resolver: Callable[[str], BinaryIO] | None = None,
        maximum_processes: int = 1,
        terminate_grace_seconds: float = 1.0,
    ) -> None:
        if maximum_processes < 1:
            raise ValueError("maximum_processes must be positive")
        self.roots = tuple(Path(root).resolve() for root in roots)
        self.artifacts = artifacts
        self.executable_paths = tuple(Path(path).resolve() for path in executable_paths)
        self.inherited_environment = dict(inherited_environment or os.environ)
        self.secret_resolver = secret_resolver
        self.stdin_resolver = stdin_resolver
        self.terminate_grace_seconds = terminate_grace_seconds
        self._slots = threading.BoundedSemaphore(maximum_processes)
        self._output_descriptors: dict[tuple[str, str, str], ArtifactDescriptor] = {}

    def execute(
        self,
        request: ProcessRequest,
        decision: PolicyDecision,
        cancellation: Cancellation,
    ) -> ExecutionResult:
        started = time.monotonic()
        rejection = self._validate_policy(request, decision)
        if rejection is not None:
            return self._result(request, started, failure=rejection)
        if cancellation.cancelled():
            return self._result(
                request,
                started,
                failure=self._failure(StableFailureCode.CANCELLED, "process was cancelled"),
                status="cancelled",
            )
        timeout = request.timeout_seconds
        if not self._slots.acquire(blocking=False):
            return self._result(
                request,
                started,
                failure=self._failure(
                    StableFailureCode.BUDGET_EXCEEDED, "process budget exhausted"
                ),
            )
        try:
            cwd = self._resolve_cwd(request.cwd)
            executable = self._resolve_executable(request.argv[0], cwd)
            environment = self._environment(request)
            argv = (str(executable), *request.argv[1:])
            stdin_handle: BinaryIO | None = None
            if request.stdin_artifact_digest is not None:
                assert self.stdin_resolver is not None
                stdin_handle = self.stdin_resolver(request.stdin_artifact_digest)
            stdin: BinaryIO | int = subprocess.DEVNULL if stdin_handle is None else stdin_handle
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=environment,
                    stdin=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    start_new_session=True,
                )
            except OSError as error:
                if stdin_handle is not None:
                    stdin_handle.close()
                return self._result(
                    request,
                    started,
                    failure=self._failure(StableFailureCode.SPAWN_FAILED, str(error)),
                )
            try:
                (
                    stdout,
                    stderr,
                    stdout_exceeded,
                    stderr_exceeded,
                    stdout_observed,
                    stderr_observed,
                    cancelled,
                    timed_out,
                    cleanup,
                ) = self._capture(process, request, cancellation, started, timeout)
            finally:
                if stdin_handle is not None:
                    stdin_handle.close()
            execution_id = identifier("execution")
            stdout_digest = self._store_output(request, execution_id, stdout, "process_stdout")
            stderr_digest = self._store_output(request, execution_id, stderr, "process_stderr")
            status: Literal["succeeded", "failed", "cancelled", "indeterminate"]
            if cancelled:
                failure = self._failure(StableFailureCode.CANCELLED, "process was cancelled")
                status = "cancelled"
            elif timed_out:
                failure = self._failure(StableFailureCode.TIMEOUT, "process timed out")
                status = "failed"
            elif stdout_exceeded:
                failure = self._failure(
                    StableFailureCode.BUDGET_EXCEEDED, "process output exceeded its byte budget"
                )
                status = "failed"
            elif process.returncode not in request.expected_exit_codes:
                failure = self._failure(
                    StableFailureCode.PROCESS_FAILED,
                    f"process exited with code {process.returncode}",
                )
                status = "failed"
            else:
                failure = None
                status = "succeeded"
            return ExecutionResult(
                id=execution_id,
                run_id=request.run_id,
                created_at=now(),
                request_digest=request.content_digest or "",
                status=status,
                failure=failure,
                exit_code=process.returncode,
                duration_seconds=time.monotonic() - started,
                resource_usage=freeze_json(
                    {
                        "argv_sha256": __import__("hashlib")
                        .sha256("\0".join(argv).encode())
                        .hexdigest(),
                        "policy_digest": decision.effective_policy_digest,
                        "stdout_bytes": stdout_observed,
                        "stderr_bytes": stderr_observed,
                        "stdout_retained_bytes": len(stdout),
                        "stderr_retained_bytes": len(stderr),
                        "stdout_truncated": stdout_exceeded,
                        "stderr_truncated": stderr_exceeded,
                        "process_group_cleanup": cleanup,
                    }
                ),
                stdout_artifact_digest=stdout_digest,
                stderr_artifact_digest=stderr_digest,
            )
        except _ProcessGroupCleanupError as error:
            return self._result(
                request,
                started,
                failure=self._failure(StableFailureCode.PROCESS_GROUP_CLEANUP_FAILED, str(error)),
                status="indeterminate",
                resource_usage={"process_group_cleanup": "failed"},
            )
        except (OSError, ValueError) as error:
            return self._result(
                request,
                started,
                failure=self._failure(StableFailureCode.INVALID_REQUEST, str(error)),
            )
        finally:
            self._slots.release()

    def _capture(
        self,
        process: subprocess.Popen[bytes],
        request: ProcessRequest,
        cancellation: Cancellation,
        started: float,
        timeout: float,
    ) -> tuple[bytes, bytes, bool, bool, int, int, bool, bool, str]:
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        selector.register(
            process.stdout, selectors.EVENT_READ, (stdout_buffer, request.stdout_bytes)
        )
        selector.register(
            process.stderr, selectors.EVENT_READ, (stderr_buffer, request.stderr_bytes)
        )
        stdout_exceeded = stderr_exceeded = cancelled = timed_out = False
        stdout_observed = stderr_observed = 0
        cleanup = "not_required"
        while selector.get_map():
            elapsed = time.monotonic() - started
            cancelled = cancellation.cancelled()
            timed_out = elapsed >= timeout
            if stdout_exceeded and not cancelled and not timed_out and process.poll() is None:
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    outcome = self._terminate_group(process)
                    if cleanup == "not_required" or outcome != "already_exited":
                        cleanup = outcome
            elif cancelled or timed_out:
                outcome = self._terminate_group(process)
                if cleanup == "not_required" or outcome != "already_exited":
                    cleanup = outcome
            for key, _events in selector.select(timeout=0.05):
                buffer, limit = key.data
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                available = max(0, limit - len(buffer))
                buffer.extend(chunk[:available])
                if key.fileobj is process.stdout:
                    stdout_observed += len(chunk)
                    stdout_exceeded = stdout_exceeded or len(chunk) > available
                else:
                    stderr_observed += len(chunk)
                    stderr_exceeded = stderr_exceeded or len(chunk) > available
            if process.poll() is not None and not selector.get_map():
                break
        process.wait()
        selector.close()
        return (
            bytes(stdout_buffer),
            bytes(stderr_buffer),
            stdout_exceeded,
            stderr_exceeded,
            stdout_observed,
            stderr_observed,
            cancelled,
            timed_out,
            cleanup,
        )

    def _terminate_group(self, process: subprocess.Popen[bytes]) -> str:
        leader_exited = process.poll() is not None
        try:
            os.killpg(process.pid, signal.SIGKILL if leader_exited else signal.SIGTERM)
            if leader_exited:
                return "sigkill_confirmed"
            process.wait(timeout=self.terminate_grace_seconds)
            return "sigterm_confirmed"
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return "sigkill_confirmed"
        except ProcessLookupError:
            return "already_exited"
        except PermissionError:
            raise _ProcessGroupCleanupError(
                "process group cleanup could not be confirmed"
            ) from None

    def _resolve_cwd(self, relative: str) -> Path:
        candidates = tuple((root / relative).resolve() for root in self.roots)
        contained = tuple(
            candidate
            for root, candidate in zip(self.roots, candidates, strict=True)
            if (candidate == root or root in candidate.parents) and candidate.is_dir()
        )
        if len(contained) != 1:
            raise ValueError("cwd is not an existing directory in exactly one authorized root")
        return contained[0]

    def _resolve_executable(self, value: str, cwd: Path) -> Path:
        if "/" in value:
            candidate = (
                (cwd / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
            )
            allowed = any(
                candidate == root or root in candidate.parents
                for root in (*self.roots, *self.executable_paths)
            )
            if not allowed:
                raise ValueError("executable escapes authorized roots")
        else:
            located = shutil.which(value, path=os.pathsep.join(map(str, self.executable_paths)))
            if located is None:
                raise ValueError("executable was not found in the configured deterministic path")
            candidate = Path(located).resolve()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ValueError("executable is not an executable file")
        return candidate

    def _environment(self, request: ProcessRequest) -> dict[str, str]:
        environment = {"PATH": os.pathsep.join(map(str, self.executable_paths)), "LANG": "C.UTF-8"}
        for name in request.inherit_environment:
            if any(marker in name.upper() for marker in _SECRET_MARKERS):
                raise ValueError("credential-like variables require an explicit secret binding")
            if name in self.inherited_environment:
                environment[name] = self.inherited_environment[name]
        environment.update(request.environment)
        for binding in request.secret_bindings:
            if self.secret_resolver is None:
                raise ValueError("secret binding resolution is unavailable")
            environment[binding.name] = self.secret_resolver(binding.binding_ref)
        return environment

    def output_descriptor(
        self,
        artifact_digest: str,
        logical_kind: str | None = None,
        producer_execution_id: str | None = None,
    ) -> ArtifactDescriptor:
        """Resolve one exact output descriptor, rejecting digest-only ambiguity."""

        if (logical_kind is None) != (producer_execution_id is None):
            raise ValueError("output provenance must be supplied together")
        if logical_kind is not None and producer_execution_id is not None:
            try:
                return self._output_descriptors[
                    (artifact_digest, logical_kind, producer_execution_id)
                ]
            except KeyError as error:
                raise ValueError("unknown process output artifact provenance") from error
        matches = tuple(
            descriptor
            for (digest, _kind, _execution_id), descriptor in self._output_descriptors.items()
            if digest == artifact_digest
        )
        if len(matches) != 1:
            raise ValueError("process output artifact digest is ambiguous")
        return matches[0]

    def _store_output(
        self, request: ProcessRequest, execution_id: str, value: bytes, kind: str
    ) -> str:
        descriptor = self.artifacts.put(
            io.BytesIO(value),
            ArtifactPutRequest(
                id=identifier("artifact-request"),
                run_id=request.run_id,
                created_at=now(),
                media_type="application/octet-stream",
                logical_kind=kind,
                producer_action_id=request.id,
                source=freeze_json(
                    {
                        "request_digest": request.content_digest,
                        "execution_id": execution_id,
                        "bounded": True,
                    }
                ),
                redacted=bool(request.secret_bindings),
            ),
        )
        self._output_descriptors[(descriptor.artifact_digest, kind, execution_id)] = descriptor
        return descriptor.artifact_digest

    def _validate_policy(
        self, request: ProcessRequest, decision: PolicyDecision
    ) -> StableFailure | None:
        if decision.request_digest != request.content_digest:
            return self._failure(
                StableFailureCode.POLICY_DENIED, "decision/request digest mismatch"
            )
        if decision.outcome is DecisionOutcome.DENY:
            return self._failure(StableFailureCode.POLICY_DENIED, "process denied by policy")
        if decision.outcome is DecisionOutcome.APPROVAL_REQUIRED:
            return self._failure(StableFailureCode.APPROVAL_REQUIRED, "process requires approval")
        limits = decision.limits if isinstance(decision.limits, Mapping) else {}
        maximum_wall = float(limits.get("max_wall_seconds", request.timeout_seconds))
        maximum_artifact = int(
            limits.get("max_artifact_bytes", max(request.stdout_bytes, request.stderr_bytes))
        )
        if (
            request.timeout_seconds > maximum_wall
            or request.stdout_bytes > maximum_artifact
            or request.stderr_bytes > maximum_artifact
            or int(limits.get("max_processes", 1)) < 1
        ):
            return self._failure(
                StableFailureCode.BUDGET_EXCEEDED,
                "process request exceeds the effective policy budget",
            )
        if request.stdin_artifact_digest is not None and self.stdin_resolver is None:
            return self._failure(
                StableFailureCode.INVALID_REQUEST,
                "stdin artifacts are unavailable without a descriptor-bound resolver",
            )
        return None

    def _result(
        self,
        request: ProcessRequest,
        started: float,
        *,
        failure: StableFailure,
        status: Literal["failed", "cancelled", "indeterminate"] = "failed",
        resource_usage: object = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            id=identifier("execution"),
            run_id=request.run_id,
            created_at=now(),
            request_digest=request.content_digest or "",
            status=status,
            failure=failure,
            duration_seconds=time.monotonic() - started,
            resource_usage=freeze_json(resource_usage),
        )

    @staticmethod
    def _failure(code: StableFailureCode, message: str) -> StableFailure:
        return StableFailure(code=code, message=message)
