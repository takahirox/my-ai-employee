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
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


def candidate_archive(root: Path, limit: int) -> bytes:
    """Export the prepared workspace as regular files, never host runtime metadata."""
    names = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(
            part in {".git", ".fleet", ".codex", ".claude", ".agents"} for part in relative.parts
        ):
            continue
        if path.is_symlink():
            raise ValueError("workspace archive does not accept symlinks")
        if not path.is_dir():
            names.append(relative.as_posix())
    output = io.BytesIO()
    size = 0
    directories: set[str] = set()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name in names:
            if not name:
                continue
            path = Path(name)
            target = root / path
            if path.is_absolute() or ".." in path.parts or path.parts[0] == ".git":
                raise ValueError("unsafe candidate path")
            if not target.exists() and not target.is_symlink():
                continue
            if any(
                p.is_symlink() for p in [target, *target.parents] if p != root and root in p.parents
            ):
                raise ValueError("initial isolated profile does not support candidate symlinks")
            if not target.is_file():
                raise ValueError("candidate must contain regular files (no submodules/devices)")
            content = target.read_bytes()
            size += len(content)
            if size > limit:
                raise ValueError("candidate export exceeds isolated workspace byte limit")
            entry = tarfile.TarInfo(name)
            for parent in reversed(path.parents):
                if str(parent) == "." or str(parent) in directories:
                    continue
                directory = tarfile.TarInfo(str(parent))
                directory.type, directory.uid, directory.gid = tarfile.DIRTYPE, 1000, 1000
                directory.mode = 0o755
                archive.addfile(directory)
                directories.add(str(parent))
            entry.size = len(content)
            entry.uid = entry.gid = 1000
            entry.mode = 0o755 if target.stat().st_mode & 0o111 else 0o644
            archive.addfile(entry, io.BytesIO(content))
    return output.getvalue()


class IsolatedBudgetExceeded(RuntimeError):
    """An enforced isolated resource budget has been exhausted."""


class NativeProcessBudgetExceeded(IsolatedBudgetExceeded):
    """No further native work or candidate submission is authorized."""


def resource_missing(kind: str, name: str, error: bytes) -> bool:
    message = error.decode(errors="replace").lower()
    return f"no such {kind}: {name}" in message or (
        kind == "network" and f"network {name} not found" in message
    )


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
        seconds: float,
        cancellation: Cancellation,
        output_limit: int = 1_000_000,
        resource_ledger: Path | None = None,
        service_hosts: tuple[str, ...] = (),
    ) -> None:
        self.profile, self.root, self.cancellation = profile, root.resolve(), cancellation
        self.deadline = time.monotonic() + seconds
        self.output_limit = output_limit
        self.resource_ledger = resource_ledger
        self.service_hosts = service_hosts
        self.name = "fleet-candidate-" + uuid.uuid4().hex
        self.created = False
        self.network: str | None = None
        self.proxy: str | None = None
        self.native_process_usage: dict[str, object] = {}
        self.confirmed_resources: set[tuple[str, str]] = set()

    def _record_resource(self, kind: str, name: str, state: str = "intent") -> None:
        """Operator-only crash-recovery ledger; never copied into the worker."""
        if state == "created":
            self.confirmed_resources.add((kind, name))
        append_resource_event(self.resource_ledger, kind, name, state)

    def _docker(self, *args: str, data: bytes | None = None) -> bytes:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.cancellation.cancelled():
            raise TimeoutError("isolated candidate cancelled or deadline exhausted")
        result = subprocess.run(
            ["docker", *args],
            input=data,
            capture_output=True,
            timeout=min(30, remaining),
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Docker operation {args[0]} failed: "
                + result.stderr.decode(errors="replace")[:1000]
            )
        if args[0] in ("create", "run") and "--name" in args:
            self._record_resource("container", args[args.index("--name") + 1], "created")
        elif args[:2] == ("network", "create"):
            self._record_resource("network", args[-1], "created")
        return result.stdout

    def __enter__(self) -> DockerCandidate:
        try:
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
                "import time; time.sleep(" + repr(max(1.0, self.deadline - time.monotonic())) + ")",
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
                data=candidate_archive(
                    self.root,
                    self.profile.workspace_mb * 1024**2,
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
    ) -> tuple[int, bytes, bytes]:
        """Stream native events with a bounded output; cancellation kills the whole container."""
        import selectors

        if self.cancellation.cancelled() or time.monotonic() >= self.deadline:
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
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, stdout)
                selector.register(process.stderr, selectors.EVENT_READ, stderr)
                while selector.get_map():
                    if self.cancellation.cancelled() or time.monotonic() >= self.deadline:
                        raise TimeoutError("isolated execution cancelled or timed out")
                    if supervise is not None:
                        supervise(len(stdout) + len(stderr))
                    for key, _ in selector.select(timeout=0.05):
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        key.data.extend(data)
                        if len(stdout) + len(stderr) > self.output_limit:
                            raise IsolatedBudgetExceeded(
                                "isolated execution output budget exceeded"
                            )
                        if observe and key.fileobj is process.stdout:
                            pending.extend(data)
                            while b"\n" in pending:
                                line, _, tail = pending.partition(b"\n")
                                pending = bytearray(tail)
                                try:
                                    event = json.loads(line)
                                except ValueError:
                                    continue
                                if isinstance(event, dict):
                                    observe(event)
            return process.wait(timeout=2), bytes(stdout), bytes(stderr)
        except BaseException:
            try:
                self.close()
            finally:
                process.kill()
                process.wait(timeout=5)
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

    def close(self) -> None:
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
