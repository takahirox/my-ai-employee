"""Native Worker sessions inside Fleet's existing disposable process namespace.

The outer container supplies resource/lifetime boundaries that native macOS
sandboxing lacks. The native sandbox still controls autonomous tool execution.
No proposed edit is reconstructed: the quiesced workspace is copied verbatim.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from . import snapshot
from .candidates import Candidates
from .command_diagnostics import command_event
from .diagnostics import CheckOutput, attach_failure
from .history import Stopped
from .isolated_worker import (
    DockerCandidate,
    IsolatedWorkerProfile,
    append_resource_event,
)
from .models import Authority, Check, StagePolicy, Usage, WorkerResult
from .native import (
    ModelAtCapacity,
    T,
    capacity_error,
    codex_permissions,
    decode_response,
    measured_usage,
    provider_schema,
    quota_error,
    summarize_event,
)
from .owner_watch import resource_missing
from .product_capabilities import (
    CODEX_VERSION,
    LOCAL_SERVICE_DIRECTORY,
    NATIVE_PATH,
    execution_environment,
)
from .snapshot import LEGACY_SNAPSHOT_BYTES, unpack_workspace
from .time_budget import exhausted, minimum, remaining

_OFFLINE = Authority()


class Cancellation:
    def __init__(self, callback: Callable[[], bool]) -> None:
        self.callback = callback

    def cancelled(self) -> bool:
        return self.callback()


class ContainerModel:
    def __init__(
        self,
        profile: IsolatedWorkerProfile | None,
        *,
        snapshot_max_bytes: int = LEGACY_SNAPSHOT_BYTES,
    ) -> None:
        self.profile = profile
        self.snapshot_max_bytes = snapshot_max_bytes

    def preflight(
        self,
        policy: StagePolicy,
        workspace: Path,
        authority: Authority,
        timeout: float | None,
        cancelled: Callable[[], bool],
        checks: tuple[Check, ...] = (),
    ) -> dict[str, Any]:
        from .capabilities import authority_supported, validate_policy

        validate_policy(policy)
        authority_supported(authority)
        if self.profile is None:
            raise Stopped("EXPLICIT_PROCESS_ISOLATION_PROFILE_REQUIRED")
        auth = self.profile.auth_file
        if auth is None or not Path(auth).is_file():
            raise Stopped("EXPLICIT_DELEGATED_MODEL_AUTH_REQUIRED")
        # Actual image/native boundary, without an LLM request or service mutation.
        # Never cache across environment replacement, invocation or authority change.
        with self._candidate(workspace, timeout, cancelled, models=False, authority=authority) as c:
            version = c._docker("exec", c.name, "codex", "--version").decode().strip()
            if version != CODEX_VERSION:
                raise Stopped("UNSUPPORTED_NATIVE_VERSION")
            c._docker(
                "exec",
                "--env",
                f"PATH={NATIVE_PATH}",
                c.name,
                "python",
                "-I",
                "-c",
                "import shutil,sys; "
                "sys.exit(any(shutil.which(name) is None for name in sys.argv[1:]))",
                *(check.argv[0] for check in checks),
            )
            self._native_probe(c, authority)
            return {
                "version": version,
                "backend": policy.backend,
                "image": c.profile.image,
                "environment": c.name,
                "authority_digest": authority.digest,
                "available": True,
                "scope": "probe_only; invocation_rechecks",
            }

    @staticmethod
    def _validate(authority: Authority) -> None:
        from .capabilities import authority_supported

        try:
            authority_supported(authority)
        except Stopped as error:
            raise ValueError(str(error)) from None

    @contextmanager
    def _candidate(
        self,
        workspace: Path,
        timeout: float | None,
        cancelled: Callable[[], bool],
        *,
        models: bool,
        retain_partial: Callable[[dict[str, Any]], None] | None = None,
        authority: Authority = _OFFLINE,
    ) -> Iterator[DockerCandidate]:
        if self.profile is None:
            raise ValueError("EXPLICIT_PROCESS_ISOLATION_PROFILE_REQUIRED")
        profile = self.profile.model_copy(
            update={
                "auth_file": self.profile.auth_file if models else None,
            }
        )
        candidate = DockerCandidate(
            profile,
            workspace,
            seconds=timeout,
            cancellation=Cancellation(cancelled),
            resource_ledger=workspace.parent / (workspace.name + ".resources.jsonl"),
            service_hosts=authority.network_hosts,
            output_limit=8_000_000,
            snapshot_max_bytes=self.snapshot_max_bytes,
        )
        try:
            with candidate:
                try:
                    yield candidate
                except BaseException as error:
                    # Capture eligibility while the stopped environment still exists.
                    with suppress(Exception):
                        if "termination" not in candidate.execution_diagnostic:
                            candidate.record_termination(error)
                    if retain_partial is not None:
                        self._retain_workspace(candidate, workspace, retain_partial)
                    raise
        except BaseException as error:
            with suppress(Exception):
                if "termination" not in candidate.execution_diagnostic:
                    candidate.record_termination(error)
                if candidate.execution_diagnostic["termination"]["reason"] == "completed":
                    candidate.execution_diagnostic["termination"]["reason"] = "cleanup_failure"
                candidate.execution_diagnostic["termination"]["disposal"] = candidate.disposal
            attach_failure(error, candidate.execution_diagnostic)
            raise

    @staticmethod
    def _native_probe(candidate: DockerCandidate, authority: Authority = _OFFLINE) -> None:
        candidate.begin_execution("probe")
        # A user-readable canary outside /work distinguishes native filesystem
        # confinement from the outer container's ordinary Unix permissions.
        candidate._docker(
            "exec",
            candidate.name,
            "python",
            "-I",
            "-c",
            "from pathlib import Path; "
            "[Path('/work',name).mkdir(exist_ok=True) for name in "
            "('.agents','.codex','.claude','.fleet','.fleet-inputs')]",
        )
        candidate._docker(
            "exec",
            "--user",
            "1000:1000",
            candidate.name,
            "python",
            "-I",
            "-c",
            "from pathlib import Path; Path('/home/fleet/native-canary').write_text('disposable')",
        )
        command = (
            "codex",
            *codex_permissions(
                Path("/work"),
                authority,
                local_service_storage_mb=candidate.profile.local_service_storage_mb,
            ),
            "sandbox",
            "--permission-profile",
            "fleet-worker",
            "--cd",
            "/work",
            "--",
            "/usr/bin/python3",
            "-I",
            "-c",
            "from pathlib import Path\n"
            "import os,sys\n"
            f"p=Path('/work/.fleet-probe-{uuid4().hex}'); p.open('x').close(); p.unlink()\n"
            "try: Path('/home/fleet/native-canary').read_text()\n"
            "except (PermissionError, FileNotFoundError): pass\n"
            "else: raise AssertionError('native read boundary unavailable')\n"
            "try:\n"
            " status=Path('/proc/self/status').read_text()\n"
            " assert f'Pid:\\t{os.getpid()}\\n' in status\n"
            " assert Path('/proc/self/smaps').read_text()\n"
            " assert not Path('/proc/sys').exists()\n"
            "except (OSError, AssertionError): sys.exit(78)\n"
            + (
                "try:\n"
                " import socket\n"
                f" p=Path({LOCAL_SERVICE_DIRECTORY!r})/'.readiness'; "
                "p.write_text('probe'); p.unlink()\n"
                " with socket.socket() as server:\n"
                "  server.bind(('127.0.0.1',0)); server.listen(1)\n"
                "  with socket.create_connection(server.getsockname(),timeout=1) as client:\n"
                "   connection,_=server.accept(); connection.close()\n"
                "except OSError: sys.exit(79)\n"
                if candidate.profile.local_service_storage_mb is not None
                else ""
            ),
        )
        code, _, _ = candidate.run_guarded(command, timeout=15, phase="probe")
        if code == 78:
            raise ValueError("NATIVE_RUNTIME_PROCFS_UNAVAILABLE")
        if code == 79:
            raise ValueError("LOCAL_SERVICE_SANDBOX_UNAVAILABLE")
        if code:
            raise ValueError("NATIVE_SANDBOX_PREFLIGHT_FAILED")

    def _copy_workspace(self, candidate: DockerCandidate, workspace: Path) -> None:
        candidate.quiesce()
        # Execute the same stdlib-only snapshot contract inside the container.
        # No task writer remains, and the source comes from the controller.
        program = Path(snapshot.__file__).read_text() + (
            "\nimport sys\nsys.stdout.buffer.write(pack_workspace(Path('/work'), "
            f"{self.snapshot_max_bytes}))\n"
        )
        data = candidate._docker("exec", candidate.name, "python", "-I", "-c", program)
        with TemporaryDirectory(prefix="fleet-return-", dir=workspace.parent) as directory:
            returned = Path(directory)
            unpack_workspace(data, returned, self.snapshot_max_bytes)
            # Validate candidate identity before replacing this Run-owned workspace.
            with TemporaryDirectory(prefix="fleet-return-check-") as objects:
                Candidates(Path(objects), max_bytes=self.snapshot_max_bytes).capture(returned)
            for child in workspace.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            for child in returned.iterdir():
                shutil.move(str(child), workspace / child.name)

    def _retain_workspace(
        self,
        candidate: DockerCandidate,
        workspace: Path,
        observation: Callable[[dict[str, Any]], None],
    ) -> None:
        """Best-effort extraction at the shared pre-disposal boundary; never restart work."""
        outcome = {"status": "unavailable", "reason": "control_unavailable"}
        candidate.execution_diagnostic["partial_workspace"] = outcome
        deadline = candidate.deadline
        try:
            if candidate.phase != "model":
                outcome["reason"] = "worker_not_started"
                return
            outcome["reason"] = candidate.retention_status()
            if candidate.execution_diagnostic.get("workspace_returned"):
                outcome.update(status="ready", reason="already_returned")
                return
            if "workspace_returned" in candidate.execution_diagnostic:
                outcome.update(status="capture_failed", reason="workspace_transfer_failed")
                return
            if outcome["reason"] != "eligible":
                return
            candidate.deadline = minimum(deadline, time.monotonic() + 30)
            with TemporaryDirectory(prefix="fleet-partial-", dir=workspace.parent) as directory:
                partial = Path(directory)
                self._copy_workspace(candidate, partial)
                if candidate.retention_status() != "eligible":
                    raise TimeoutError("PARTIAL_EXTRACTION_INTERRUPTED")
                # The engine stores the immutable tree while this private staging
                # directory exists. Never replace the live retry workspace.
                observation({"event": "partial_workspace", "workspace": str(partial)})
                outcome.update(status="ready", reason="extracted")
        except Exception as error:
            outcome.update(status="capture_failed", reason=type(error).__name__)
        finally:
            candidate.deadline = deadline

    def reconcile(self, run_directory: Path) -> None:
        """Reconcile only owned resource ledgers before any continuation or revocation."""
        if not run_directory.exists():
            return
        uncertain = False
        for ledger in sorted(run_directory.glob("*.resources.jsonl")):
            if ledger.is_symlink() or ledger.stat().st_size > 100_000:
                raise ValueError("INVALID_RESOURCE_LEDGER")
            latest: dict[tuple[str, str], str] = {}
            for line in ledger.read_text().splitlines():
                item = json.loads(line)
                kind, name, state = item.get("kind"), item.get("name"), item.get("state")
                suffix = "-network" if kind == "network" else "(?:-proxy)?"
                if (
                    kind not in {"container", "network"}
                    or not isinstance(name, str)
                    or re.fullmatch("fleet-candidate-[0-9a-f]{32}" + suffix, name) is None
                    or state not in {"intent", "created", "removed"}
                ):
                    raise ValueError("INVALID_RESOURCE_LEDGER")
                latest[kind, name] = state
            uncertain |= "intent" in latest.values()
            for kind, name in sorted(latest, key=lambda item: item[0] == "network"):
                if latest[kind, name] == "removed":
                    continue
                try:
                    result = subprocess.run(
                        ["docker", kind, "rm", *(["-f"] if kind == "container" else []), name],
                        capture_output=True,
                        timeout=15,
                    )
                except subprocess.TimeoutExpired as error:
                    raise ValueError("RESOURCE_CLEANUP_UNCONFIRMED") from error
                if result.returncode and not resource_missing(kind, name, result.stderr):
                    raise ValueError("RESOURCE_CLEANUP_UNCONFIRMED")
                if latest[kind, name] == "created":
                    append_resource_event(ledger, kind, name, "removed")
        if uncertain:
            raise ValueError("RESOURCE_CREATION_UNCERTAIN")

    def apply_authority(
        self,
        workspace: Path,
        authority: Authority,
        timeout: float | None,
        cancelled: Callable[[], bool],
    ) -> None:
        self._validate(authority)
        with self._candidate(
            workspace, minimum(30, timeout), cancelled, models=False, authority=authority
        ) as candidate:
            self._native_probe(candidate, authority)

    def generate(
        self,
        policy: StagePolicy,
        prompt: str,
        schema: type[T],
        workspace: Path,
        authority: Authority,
        timeout: float | None,
        cancelled: Callable[[], bool],
        observer: Callable[[float, int], None] | None = None,
        observation: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[T, Usage]:
        self._validate(authority)
        if policy.backend != "codex":
            raise ValueError("NATIVE_BACKEND_ATTESTATION_UNAVAILABLE")
        if self.profile is None:
            raise ValueError("EXPLICIT_PROCESS_ISOLATION_PROFILE_REQUIRED")
        if self.profile.auth_file is None:
            raise ValueError("EXPLICIT_DELEGATED_MODEL_AUTH_REQUIRED")
        began = time.monotonic()
        supervised = began
        observation_count = 0

        def observe(event: dict[str, object]) -> None:
            nonlocal observation_count
            encoded = json.dumps(event)
            if quota_error(encoded):
                if observation is not None:
                    observation({"event": "usage_limit", "source": "provider"})
                raise Stopped("USAGE_LIMIT")
            if event.get("type") == "turn.completed" and observation is not None:
                observation(
                    {"event": "usage_observed", **measured_usage(event.get("usage")).model_dump()}
                )
            # Payload capture is separate from capped activity metadata and never
            # participates in quota classification, model decoding or authority.
            if observation is not None:
                try:
                    detail = command_event(event, time.monotonic() - began)
                    if detail is not None:
                        observation(detail)
                except Exception:
                    observation({"event": "command_capture_failed"})
            summary = summarize_event(encoded)
            if observation is not None and summary is not None and observation_count < 1000:
                observation(summary)
                observation_count += 1

        def supervise(output_bytes: int) -> None:
            nonlocal supervised
            if (
                observer is not None
                and policy.supervision_seconds is not None
                and time.monotonic() - supervised >= policy.supervision_seconds
            ):
                supervised = time.monotonic()
                observer(supervised - began, output_bytes)

        with self._candidate(
            workspace,
            timeout,
            cancelled,
            models=True,
            authority=authority,
            retain_partial=observation if schema is WorkerResult else None,
        ) as candidate:
            self._native_probe(candidate, authority)
            candidate.begin_execution("model")
            if observation is not None:
                observation(
                    {
                        "event": "environment_applied",
                        "image": candidate.profile.image,
                        "namespace": candidate.name,
                        "authority_digest": authority.digest,
                        "sandbox": "codex-native",
                    }
                )
            body = json.loads(prompt)
            body["execution_workspace"] = "/work"
            body["execution_environment"] = execution_environment(
                candidate.profile.local_service_storage_mb
            )
            if isinstance(body.get("context"), dict):
                body["context"]["workspace"] = "/work"
            schema_path = "/tmp/fleet-output-schema.json"
            candidate._docker(
                "exec",
                "-i",
                candidate.name,
                "python",
                "-I",
                "-c",
                "import sys; from pathlib import Path; "
                "Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())",
                schema_path,
                data=json.dumps(provider_schema(schema, body.get("stage_contract"))).encode(),
            )
            command = (
                "codex",
                "--ask-for-approval",
                "never",
                "exec",
                "--json",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--model",
                policy.model,
                "-c",
                "model_reasoning_effort=" + json.dumps(policy.effort),
                *codex_permissions(
                    Path("/work"),
                    authority,
                    local_service_storage_mb=candidate.profile.local_service_storage_mb,
                ),
                "--cd",
                "/work",
                "--output-schema",
                schema_path,
                "-",
            )
            # Native setup consumes the same deadline as the actual model process.
            seconds_left = remaining(candidate.deadline)
            if exhausted(seconds_left) or cancelled():
                raise TimeoutError("NATIVE_SETUP_TIMEOUT")
            if "execution_budget" in body:
                body["execution_budget"]["reserved_active_seconds"] = seconds_left
            if observation is not None:
                observation(
                    {"event": "execution_budget", "phase": "native_launch", "seconds": seconds_left}
                )
            prompt = json.dumps(body, ensure_ascii=False)
            code, stdout, stderr = candidate.run_guarded(
                command,
                stdin=prompt.encode(),
                phase="model",
                observe=observe,
                supervise=supervise,
            )
            for line in (stdout + b"\n" + stderr).decode(errors="replace").splitlines():
                if quota_error(line, stderr=True):
                    if observation is not None:
                        observation({"event": "usage_limit", "source": "provider"})
                    raise Stopped("USAGE_LIMIT")
            if candidate.proxy and observation is not None:
                logs = candidate._docker("logs", "--tail", "1000", candidate.proxy)
                for line in logs.decode(errors="replace").splitlines():
                    item = json.loads(line)
                    observation(
                        {
                            "event": "network",
                            "destination": item["destination"],
                            "destination_digest": item["destination_digest"],
                            "allowed": item["allowed"],
                        }
                    )
            if code:
                if capacity_error(stdout.decode(errors="replace")):
                    raise ModelAtCapacity("MODEL_AT_CAPACITY")
                raise ValueError("WORKER_PROCESS_FAILED")
            result = decode_response(stdout.decode(errors="replace"), schema)
            candidate.execution_diagnostic["workspace_returned"] = False
            self._copy_workspace(candidate, workspace)
            candidate.execution_diagnostic["workspace_returned"] = True
            candidate.record_termination(None)
        # Keep completion facts available for Engine-side response validation errors.
        # The engine retains this in memory; successful calls need no extra journal event.
        candidate.execution_diagnostic["termination"]["disposal"] = candidate.disposal
        if observation is not None:
            observation(
                {"event": "execution_termination", "snapshot": candidate.execution_diagnostic}
            )
        return result

    def check(
        self,
        argv: tuple[str, ...],
        workspace: Path,
        timeout: float | None,
        cancelled: Callable[[], bool],
    ) -> tuple[bool, CheckOutput]:
        with self._candidate(workspace, timeout, cancelled, models=False) as candidate:
            self._native_probe(candidate)
            candidate.begin_execution("check")
            command = (
                "codex",
                *codex_permissions(
                    Path("/work"),
                    Authority(),
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
            code, stdout, stderr = candidate.run_guarded(command, phase="check")
            return code == 0, CheckOutput(code, stdout, stderr)
