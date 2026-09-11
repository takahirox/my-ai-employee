"""Native Worker sessions inside Fleet's existing disposable process namespace.

The outer container supplies resource/lifetime boundaries that native macOS
sandboxing lacks. The native sandbox still controls autonomous tool execution.
No proposed edit is reconstructed: the quiesced workspace is copied verbatim.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from .candidates import Candidates
from .diagnostics import CheckOutput
from .history import Stopped
from .isolated_worker import (
    DockerCandidate,
    IsolatedWorkerProfile,
    append_resource_event,
    resource_missing,
)
from .models import Authority, Check, StagePolicy, Usage
from .native import (
    T,
    codex_permissions,
    decode_response,
    measured_tokens,
    provider_schema,
    quota_error,
    summarize_event,
)
from .product_capabilities import CODEX_VERSION

_OFFLINE = Authority()


class Cancellation:
    def __init__(self, callback: Callable[[], bool]) -> None:
        self.callback = callback

    def cancelled(self) -> bool:
        return self.callback()


class ContainerModel:
    def __init__(self, profile: IsolatedWorkerProfile | None) -> None:
        self.profile = profile

    def preflight(
        self,
        policy: StagePolicy,
        workspace: Path,
        authority: Authority,
        timeout: float,
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

    def _candidate(
        self,
        workspace: Path,
        timeout: float,
        cancelled: Callable[[], bool],
        *,
        models: bool,
        authority: Authority = _OFFLINE,
    ) -> DockerCandidate:
        if self.profile is None:
            raise ValueError("EXPLICIT_PROCESS_ISOLATION_PROFILE_REQUIRED")
        profile = self.profile.model_copy(
            update={
                "auth_file": self.profile.auth_file if models else None,
            }
        )
        return DockerCandidate(
            profile,
            workspace,
            seconds=timeout,
            cancellation=Cancellation(cancelled),
            resource_ledger=workspace.parent / (workspace.name + ".resources.jsonl"),
            service_hosts=authority.network_hosts,
            output_limit=8_000_000,
        )

    @staticmethod
    def _native_probe(candidate: DockerCandidate, authority: Authority = _OFFLINE) -> None:
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
            *codex_permissions(Path("/work"), authority),
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
            f"p=Path('/work/.fleet-probe-{uuid4().hex}'); p.open('x').close(); p.unlink()\n"
            "try: Path('/home/fleet/native-canary').read_text()\n"
            "except (PermissionError, FileNotFoundError): pass\n"
            "else: raise AssertionError('native read boundary unavailable')\n",
        )
        code, _, _ = candidate.run_guarded(
            command, process_limit=candidate.profile.native_process_limit
        )
        if code:
            raise ValueError("NATIVE_SANDBOX_PREFLIGHT_FAILED")

    @staticmethod
    def _copy_workspace(candidate: DockerCandidate, workspace: Path) -> None:
        candidate.quiesce()
        # No task writer remains. Bound regular-file data before streaming it
        # out; archive metadata is regenerated rather than trusted from the task.
        program = """
import io,os,stat,sys,tarfile
from pathlib import Path
root=Path('/work'); files=[]; size=0
for directory,dirs,names in os.walk(root,followlinks=False):
    dirs[:]=sorted(d for d in dirs if d not in {'.git','.fleet','.codex','.claude','.agents'})
    for name in dirs:
        if (Path(directory)/name).is_symlink(): raise ValueError('snapshot symlink')
    for name in sorted(names):
        path=Path(directory)/name
        if name in {'.git','.fleet','.codex','.claude','.agents'}: continue
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode): raise ValueError('snapshot special file')
        size+=info.st_size
        if size>64000000 or len(files)>=10000: raise ValueError('snapshot budget')
        files.append((path,info.st_mode))
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:
    for path,mode in files:
        data=path.read_bytes(); info=tarfile.TarInfo(path.relative_to(root).as_posix())
        info.size=len(data); info.mode=0o700 if mode&0o111 else 0o600
        archive.addfile(info,io.BytesIO(data))
"""
        data = candidate._docker("exec", candidate.name, "python", "-I", "-c", program)
        if len(data) > 80_000_000:
            raise ValueError("CANDIDATE_TRANSPORT_SIZE_LIMIT")
        with TemporaryDirectory(prefix="fleet-return-", dir=workspace.parent) as directory:
            returned = Path(directory)
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                for member in archive:
                    relative = Path(member.name)
                    if not member.isfile() or relative.is_absolute() or ".." in relative.parts:
                        raise ValueError("CANDIDATE_TRANSPORT_UNSAFE_PATH")
                    target = returned / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    stream = archive.extractfile(member)
                    assert stream is not None
                    target.write_bytes(stream.read())
                    target.chmod(0o700 if member.mode & 0o111 else 0o600)
            # Validate regular bytes before replacing this Run-owned workspace.
            with TemporaryDirectory(prefix="fleet-return-check-") as objects:
                Candidates(Path(objects)).capture(returned)
            for child in workspace.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            for child in returned.iterdir():
                shutil.move(str(child), workspace / child.name)

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
        self, workspace: Path, authority: Authority, timeout: float, cancelled: Callable[[], bool]
    ) -> None:
        self._validate(authority)
        with self._candidate(
            workspace, min(30, timeout), cancelled, models=False, authority=authority
        ) as candidate:
            self._native_probe(candidate, authority)

    def generate(
        self,
        policy: StagePolicy,
        prompt: str,
        schema: type[T],
        workspace: Path,
        authority: Authority,
        timeout: float,
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
                    {"event": "usage_observed", "tokens": measured_tokens(event.get("usage"))}
                )
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
            workspace, timeout, cancelled, models=True, authority=authority
        ) as candidate:
            self._native_probe(candidate, authority)
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
            if isinstance(body.get("context"), dict):
                body["context"]["workspace"] = "/work"
            prompt = json.dumps(body, ensure_ascii=False)
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
                *codex_permissions(Path("/work"), authority),
                "--cd",
                "/work",
                "--output-schema",
                schema_path,
                "-",
            )
            code, stdout, stderr = candidate.run_guarded(
                command,
                process_limit=candidate.profile.native_process_limit,
                stdin=prompt.encode(),
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
            self._copy_workspace(candidate, workspace)
            if code:
                raise ValueError("WORKER_PROCESS_FAILED")
            return decode_response(stdout.decode(errors="replace"), schema)

    def check(
        self, argv: tuple[str, ...], workspace: Path, timeout: float, cancelled: Callable[[], bool]
    ) -> tuple[bool, CheckOutput]:
        with self._candidate(workspace, timeout, cancelled, models=False) as candidate:
            self._native_probe(candidate)
            command = (
                "codex",
                *codex_permissions(Path("/work"), Authority()),
                "sandbox",
                "--permission-profile",
                "fleet-worker",
                "--cd",
                "/work",
                "--",
                *argv,
            )
            code, stdout, stderr = candidate.run_guarded(
                command,
                process_limit=candidate.profile.native_process_limit,
            )
            return code == 0, CheckOutput(code, stdout, stderr)
