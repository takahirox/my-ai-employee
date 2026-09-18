"""Disposable Docker candidates. No host bind mounts, Docker socket, or host Git state."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import nullcontext, suppress
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import owner_watch
from .diagnostics import attach_failure, execution_snapshot
from .owner_watch import resource_missing
from .snapshot import LEGACY_SNAPSHOT_BYTES, pack_workspace
from .time_budget import exhausted, minimum, remaining


class Cancellation(Protocol):
    def cancelled(self) -> bool: ...


class IsolatedWorkerProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    image: str
    cpus: float = Field(default=2.0, gt=0, le=16, allow_inf_nan=False)
    memory_mb: int = Field(default=2048, ge=256, le=16384)
    pids_limit: int = Field(default=128, ge=16, le=1024)
    native_process_limit: int = Field(default=512, ge=1, le=1000)
    workspace_mb: int = Field(default=256, ge=16, le=4096)
    auth_file: str | None = None

    @field_validator("image")
    @classmethod
    def _immutable_image(cls, value: str) -> str:
        if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            raise ValueError("isolation requires an already-built immutable Docker image ID")
        return value

    @field_validator("auth_file")
    @classmethod
    def _absolute_auth_file(cls, value: str | None) -> str | None:
        if value is not None and (not Path(value).is_absolute() or "\x00" in value):
            raise ValueError("isolated worker auth file must be an explicit absolute path")
        return value


class IsolatedBudgetExceeded(RuntimeError):
    """An enforced isolated resource budget has been exhausted."""


class NativeProcessBudgetExceeded(IsolatedBudgetExceeded):
    """No further native work or candidate submission is authorized."""


def append_resource_event(path: Path | None, kind: str, name: str, state: str) -> None:
    if path is None:
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(
            descriptor, (json.dumps({"kind": kind, "name": name, "state": state}) + "\n").encode()
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class DockerCandidate:
    """One owner-controlled lifecycle; never silently fall back to host execution."""

    def __init__(
        self,
        profile: IsolatedWorkerProfile,
        root: Path,
        *,
        seconds: float | None,
        cancellation: Cancellation,
        output_limit: int = 1_000_000,
        snapshot_max_bytes: int = LEGACY_SNAPSHOT_BYTES,
        resource_ledger: Path | None = None,
        service_hosts: tuple[str, ...] = (),
    ) -> None:
        self.profile, self.root, self.cancellation = profile, root.resolve(), cancellation
        self.deadline = None if seconds is None else time.monotonic() + seconds
        self.output_limit = output_limit
        self.snapshot_max_bytes = snapshot_max_bytes
        self.resource_ledger = resource_ledger
        self.service_hosts = service_hosts
        self.name = "fleet-candidate-" + uuid.uuid4().hex
        self.created = False
        self.network: str | None = None
        self.proxy: str | None = None
        self.native_process_usage: dict[str, object] = {}
        self.confirmed_resources: set[tuple[str, str]] = set()
        self.pending_creations: set[tuple[str, str]] = set()
        self.owner_watch: subprocess.Popen[bytes] | None = None
        self.execution_diagnostic: dict[str, Any] = {"started": False}

    def _snapshot(
        self, stdout: bytes | bytearray, stderr: bytes | bytearray, **fields: Any
    ) -> None:
        self.execution_diagnostic = {"started": fields["started"], "capture": "unavailable"}
        with suppress(Exception):
            self.execution_diagnostic = execution_snapshot(stdout, stderr, **fields)

    def _record_resource(self, kind: str, name: str, state: str = "intent") -> None:
        """Operator-only crash-recovery ledger; never copied into the worker."""
        if state == "created":
            self.confirmed_resources.add((kind, name))
            self.pending_creations.discard((kind, name))
        elif state == "intent":
            self.pending_creations.add((kind, name))
        append_resource_event(self.resource_ledger, kind, name, state)

    def _docker(self, *args: str, data: bytes | None = None) -> bytes:
        self._check_owner()
        seconds_left = remaining(self.deadline)
        if exhausted(seconds_left) or self.cancellation.cancelled():
            raise TimeoutError("isolated candidate cancelled or deadline exhausted")
        # Poll ownership/shared active consumption even during setup and capture.
        # A second call may consume the active allowance after this operation starts.
        deadline = minimum(self.deadline, time.monotonic() + 30)
        # communicate(input=...) cannot resume partial stdin writes by calling
        # communicate(None) after a timeout: CPython no longer selects stdin.
        # A private, unlinked file lets Docker consume the full bounded payload
        # while the controller continues polling cancellation and ownership.
        with tempfile.TemporaryFile() if data is not None else nullcontext() as incoming:
            if incoming is not None:
                assert data is not None
                incoming.write(data)
                incoming.seek(0)
            process = subprocess.Popen(
                ["docker", *args],
                stdin=incoming if incoming is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                while True:
                    self._check_owner()
                    if exhausted(remaining(deadline)) or self.cancellation.cancelled():
                        raise TimeoutError("DOCKER_CONTROL_TIMEOUT")
                    try:
                        stdout, stderr = process.communicate(timeout=0.05)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                process.kill()
                process.communicate(timeout=5)
                self.close()
                raise
        if process.returncode:
            raise RuntimeError(
                f"Docker operation {args[0]} failed: " + stderr.decode(errors="replace")[:1000]
            )
        if args[0] in ("create", "run") and "--name" in args:
            self._record_resource("container", args[args.index("--name") + 1], "created")
        elif args[:2] == ("network", "create"):
            self._record_resource("network", args[-1], "created")
        return stdout

    def __enter__(self) -> DockerCandidate:
        try:
            self.owner_watch = owner_watch.start(self.name)
            inspected = json.loads(self._docker("image", "inspect", self.profile.image))[0]
            if inspected["Id"] != self.profile.image or inspected["Os"] != "linux":
                raise ValueError("isolated worker requires the exact Linux runtime image")
            if self.profile.auth_file or self.service_hosts:
                self._start_model_gateway()
            self.created = True  # Also own cleanup if create's reply times out.
            self._record_resource("container", self.name)
            self._docker(
                "create",
                "--init",
                "--name",
                self.name,
                "--network",
                self.network or "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--security-opt",
                "seccomp=unconfined",
                "--pids-limit",
                str(self.profile.pids_limit),
                "--cpus",
                str(self.profile.cpus),
                "--memory",
                f"{self.profile.memory_mb}m",
                "--tmpfs",
                f"/work:rw,nosuid,nodev,size={self.profile.workspace_mb}m,mode=1777",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
                "--tmpfs",
                "/home/fleet:rw,nosuid,nodev,size=128m,mode=700,uid=1000,gid=1000",
                "--workdir",
                "/work",
                "--user",
                "0:0",
                "--entrypoint",
                "python",
                self.profile.image,
                "-I",
                "-c",
                "import threading; threading.Event().wait(" + repr(remaining(self.deadline)) + ")",
            )
            self._docker("start", self.name)
            self._docker(
                "exec",
                "-i",
                "--user",
                "1000:1000",
                self.name,
                "python",
                "-I",
                "-c",
                "import sys,tarfile; tarfile.open(fileobj=sys.stdin.buffer, mode='r|')"
                ".extractall('/work', filter='data')",
                data=pack_workspace(
                    self.root,
                    self.snapshot_max_bytes,
                    workspace_limit=self.profile.workspace_mb * 1024**2,
                ),
            )
            self._docker("exec", self.name, "mkdir", "-m", "700", "/work/.git")
            self._probe()
            if self.profile.auth_file:
                self._copy_auth()
            return self
        except BaseException:
            self.close()
            raise

    def _probe(self) -> None:
        config = json.loads(self._docker("inspect", self.name))[0]
        host = config["HostConfig"]
        if (
            host["Privileged"]
            or host.get("Binds")
            or not host["ReadonlyRootfs"]
            or host["NetworkMode"] != (self.network or "none")
            or host["CapDrop"] != ["ALL"]
        ):
            raise ValueError("container does not match the required isolation profile")
        # Prove original/Fleet/host paths are not mounted; only tmpfs mounts are allowed.
        if any(m["Type"] != "tmpfs" for m in config.get("Mounts", [])):
            raise ValueError("unexpected container mount")
        probe = (
            "import pathlib,os,socket\nassert os.getuid()==1000\n"
            "p=pathlib.Path('/work/.fleet-isolation-probe'); p.write_text('ok'); p.unlink()\n"
            "for name in ['/work/.git/config','/etc/fleet-deny','/var/run/docker.sock']:\n"
            " try: pathlib.Path(name).write_text('must-deny')\n"
            " except OSError: pass\n"
            " else: raise AssertionError('protected write allowed')\n"
            "try: socket.create_connection(('1.1.1.1',443),timeout=0.2)\n"
            "except OSError: pass\n"
            "else: raise AssertionError('direct external networking allowed')\n"
        )
        self._docker("exec", "--user", "1000:1000", self.name, "python", "-I", "-c", probe)

    def run(
        self,
        argv: tuple[str, ...],
        *,
        stdin: bytes = b"",
        observe: Callable[[dict[str, object]], None] | None = None,
        supervise: Callable[[int], None] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes, bytes]:
        """Stream native events with a bounded output; cancellation kills the whole container."""
        import selectors

        self._snapshot(b"", b"", started=False)
        deadline = minimum(self.deadline, None if timeout is None else time.monotonic() + timeout)
        self._check_owner()
        if self.cancellation.cancelled() or exhausted(remaining(deadline)):
            self.close()
            raise TimeoutError("isolated execution cancelled or timed out before launch")

        environment = [
            "--env",
            "HOME=/home/fleet",
            "--env",
            "GIT_CONFIG_GLOBAL=/dev/null",
            "--env",
            "GIT_CONFIG_COUNT=1",
            "--env",
            "GIT_CONFIG_KEY_0=safe.directory",
            "--env",
            "GIT_CONFIG_VALUE_0=/work",
        ]
        if self.proxy:
            environment += [
                "--env",
                f"HTTPS_PROXY=http://{self.proxy}:3128",
                "--env",
                f"HTTP_PROXY=http://{self.proxy}:3128",
                "--env",
                "NO_PROXY=localhost,127.0.0.1",
            ]
        input_file = tempfile.TemporaryFile()  # noqa: SIM115 -- spans streaming subprocess lifecycle
        input_file.write(stdin)
        input_file.seek(0)
        try:
            process = subprocess.Popen(
                ["docker", "exec", "-i", "--user", "1000:1000", *environment, self.name, *argv],
                stdin=input_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except BaseException:
            input_file.close()
            raise
        assert process.stdout and process.stderr
        stdout, stderr, pending = bytearray(), bytearray(), bytearray()
        began = time.monotonic()
        last_update: float | None = None
        last_event: dict[str, object] | None = None

        def observe_line(line: bytes | bytearray) -> None:
            nonlocal last_event
            try:
                event = json.loads(line)
            except ValueError:
                return
            if isinstance(event, dict) and observe is not None:
                item = event.get("item")
                last_event = {"type": event.get("type")}
                if isinstance(item, dict):
                    last_event["item"] = {
                        k: item[k] for k in ("type", "status", "exit_code") if k in item
                    }
                observe(event)

        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, stdout)
                selector.register(process.stderr, selectors.EVENT_READ, stderr)
                while selector.get_map() or process.poll() is None:
                    self._check_owner()
                    if self.cancellation.cancelled() or exhausted(remaining(deadline)):
                        raise TimeoutError("isolated execution cancelled or timed out")
                    if supervise is not None:
                        supervise(len(stdout) + len(stderr))
                    for key, _ in selector.select(timeout=0.05):
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            if observe and key.fileobj is process.stdout and pending:
                                observe_line(pending)
                                pending.clear()
                            continue
                        key.data.extend(data)
                        last_update = time.monotonic() - began
                        if len(stdout) + len(stderr) > self.output_limit:
                            raise IsolatedBudgetExceeded(
                                "isolated execution output budget exceeded"
                            )
                        if observe and key.fileobj is process.stdout:
                            pending.extend(data)
                            while b"\n" in pending:
                                line, _, tail = pending.partition(b"\n")
                                pending = bytearray(tail)
                                observe_line(line)
            code = process.wait(timeout=2)
            self._snapshot(
                stdout,
                stderr,
                started=True,
                exit_code=code,
                last_event=last_event,
                last_update_seconds=last_update,
            )
            return code, bytes(stdout), bytes(stderr)
        except BaseException as error:
            observed_exit = process.poll()
            try:
                try:
                    self.close()
                finally:
                    process.kill()
                    process.wait(timeout=5)
            finally:
                # Stop/reap before diagnostic processing, retaining already-read buffers
                # even if cleanup raises a replacement exception.
                self._snapshot(
                    stdout,
                    stderr,
                    started=True,
                    exit_code=observed_exit,
                    last_event=last_event,
                    last_update_seconds=last_update,
                )
                attach_failure(error, self.execution_diagnostic)
            raise
        finally:
            input_file.close()
            process.stdout.close()
            process.stderr.close()

    def run_guarded(
        self,
        argv: tuple[str, ...],
        *,
        process_limit: int,
        stdin: bytes = b"",
        observe: Callable[[dict[str, object]], None] | None = None,
        supervise: Callable[[int], None] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes, bytes]:
        """Admit cumulative native process creation before the syscall executes.

        The report is not trusted until the supervisor has stopped/reaped all task
        processes. Its root-owned inode/directory cannot be replaced by the worker.
        Supervisor failure never authorizes candidate capture.
        """
        from .native_process_guard import PROCESS_GUARD_SOURCE

        if type(process_limit) is not int or process_limit < 1:
            raise ValueError("native execution requires a positive reserved process budget")
        directory = "/tmp/fleet-control-" + uuid.uuid4().hex
        report = directory + "/usage.json"
        self._docker(
            "exec",
            self.name,
            "python",
            "-I",
            "-c",
            "import os,sys; os.mkdir(sys.argv[1],0o711); "
            "fd=os.open(sys.argv[2],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o666); "
            "os.fchmod(fd,0o666); os.close(fd)",
            directory,
            report,
        )
        guard_code, stdout, stderr = self.run(
            ("python", "-I", "-c", PROCESS_GUARD_SOURCE, str(process_limit), report, *argv),
            stdin=stdin,
            observe=observe,
            supervise=supervise,
            timeout=timeout,
        )
        if guard_code not in (0, 125):
            raise RuntimeError("ISOLATION_PROCESS_GUARD_FAILED: no candidate accepted")
        usage = json.loads(
            self._docker(
                "exec",
                self.name,
                "python",
                "-I",
                "-c",
                "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text()[:4096])",
                report,
            )
        )
        if (
            not isinstance(usage, dict)
            or usage.get("cleanup") != "confirmed"
            or usage.get("guard_error") is not False
            or usage.get("limit") != process_limit
            or type(usage.get("admitted")) is not int
            or not 1 <= usage["admitted"] <= process_limit
            or type(usage.get("root_exit")) is not int
            or usage.get("denied") is not (guard_code == 125)
        ):
            raise RuntimeError("ISOLATION_PROCESS_GUARD_FAILED: invalid final accounting")
        self.native_process_usage = usage
        self.execution_diagnostic["native_exit_code"] = usage["root_exit"]
        if guard_code == 125:
            raise NativeProcessBudgetExceeded(
                "BUDGET_EXCEEDED: native process admissions exhausted"
            )
        return usage["root_exit"], stdout, stderr

    def quiesce(self) -> None:
        # PID 1 and Git authority are uid 0. All candidate/worker descendants are uid 1000.
        self._docker(
            "exec",
            "--user",
            "1000:1000",
            self.name,
            "python",
            "-I",
            "-c",
            "import os,signal; os.kill(-1,signal.SIGKILL)",
        )
        probe = (
            "from pathlib import Path\n"
            "for p in Path('/proc').glob('[0-9]*/status'):\n"
            " try: s=p.read_text()\n"
            " except FileNotFoundError: continue\n"
            " assert not any(l.startswith('Uid:\\t1000\\t') for l in s.splitlines()), "
            "'worker descendants remain'\n"
        )
        self._docker("exec", self.name, "python", "-I", "-c", probe)

    def _start_model_gateway(self) -> None:
        from .model_gateway import GATEWAY_SOURCE

        self.network = self.name + "-network"
        self.proxy = self.name + "-proxy"
        self._record_resource("network", self.network)
        self._docker("network", "create", "--internal", self.network)
        self._record_resource("container", self.proxy)
        self._docker(
            "run",
            "-d",
            "--name",
            self.proxy,
            "--network",
            "bridge",
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "32",
            "--memory",
            "128m",
            "--entrypoint",
            "python",
            self.profile.image,
            "-I",
            "-c",
            GATEWAY_SOURCE,
            json.dumps(list(self.service_hosts)),
        )
        self._docker("network", "connect", "--alias", self.proxy, self.network, self.proxy)

    def _copy_auth(self) -> None:
        assert self.profile.auth_file
        path = Path(self.profile.auth_file)
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError("explicit scoped auth file is missing or invalid")
        # Docker's archive transport sets ownership without granting candidate CAP_CHOWN.
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for name in (".codex",):
                info = tarfile.TarInfo(name)
                info.type, info.uid, info.gid, info.mode = tarfile.DIRTYPE, 1000, 1000, 0o700
                archive.addfile(info)
            body = path.read_bytes()
            info = tarfile.TarInfo(".codex/auth.json")
            info.size, info.uid, info.gid, info.mode = len(body), 1000, 1000, 0o600
            archive.addfile(info, io.BytesIO(body))
        self._docker(
            "exec",
            "-i",
            "--user",
            "1000:1000",
            self.name,
            "python",
            "-I",
            "-c",
            "import sys,tarfile; tarfile.open(fileobj=sys.stdin.buffer, mode='r|')"
            ".extractall('/home/fleet', filter='data')",
            data=stream.getvalue(),
        )

    def _check_owner(self) -> None:
        if self.owner_watch is not None and self.owner_watch.poll() is not None:
            raise RuntimeError("OWNER_WATCH_LOST")

    def close(self) -> None:
        confirmed = False
        try:
            self._close_resources()
            confirmed = not self.pending_creations
        finally:
            watcher, self.owner_watch = self.owner_watch, None
            if watcher is not None:
                assert watcher.stdin
                try:
                    if confirmed:
                        watcher.stdin.write(b"D")
                        watcher.stdin.flush()
                except BrokenPipeError:
                    pass
                finally:
                    watcher.stdin.close()
                if confirmed:
                    watcher.wait(timeout=5)
                # Uncertain cleanup leaves EOF recovery running independently.

    def _close_resources(self) -> None:
        failures = []
        for kind, name in (
            ("container", self.name if self.created else None),
            ("container", self.proxy),
            ("network", self.network),
        ):
            if name:
                result = subprocess.run(
                    ["docker", kind, "rm", *(["-f"] if kind == "container" else []), name],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                if result.returncode and not resource_missing(kind, name, result.stderr):
                    failures.append(name)
                else:
                    if (kind, name) in self.confirmed_resources:
                        self._record_resource(kind, name, "removed")
                    if name == self.name:
                        self.created = False
                    elif name == self.proxy:
                        self.proxy = None
                    elif name == self.network:
                        self.network = None
        if failures:
            raise RuntimeError(
                "isolated environment cleanup could not be confirmed: " + ", ".join(failures)
            )

    def __exit__(self, *args: object) -> None:
        self.close()
