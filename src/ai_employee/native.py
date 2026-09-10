"""Native CLI sessions; Fleet supervises processes rather than individual actions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar

from .history import Stopped
from .models import Authority, Contract, StagePolicy, Usage
from .process_lifecycle import terminate_group

T = TypeVar("T", bound=Contract)
_OFFLINE = Authority()


def quota_error(line: str, *, stderr: bool = False) -> bool:
    """Classify provider errors, never tool output quoting quota-related source code."""
    markers = (
        "usage_limit_reached",
        "usage limit reached",
        "hit your usage limit",
        "insufficient_quota",
        "credit balance is too low",
        "rate_limit_error",
        "rate_limit_exceeded",
        "quota_exceeded",
        "you've hit your limit",
    )
    try:
        event = json.loads(line)
    except ValueError:
        return stderr and bool(
            re.match(
                r"^(?:error[: ]+)?(?:you(?:'ve| have) hit|usage limit|credit balance)", line.lower()
            )
        )
    if not isinstance(event, dict):
        return False
    if event.get("type") in {"error", "turn.failed"} or (
        event.get("type") == "result" and event.get("is_error") is True
    ):
        return any(marker in line.lower() for marker in markers)
    info = event.get("rate_limit_info")
    return (
        event.get("type") == "rate_limit_event"
        and isinstance(info, dict)
        and info.get("status") == "rejected"
    )


def summarize_event(line: str) -> dict[str, Any] | None:
    """Expose actual transport activity without copying commands, responses or secrets."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    if kind not in {
        "item.started",
        "item.completed",
        "turn.completed",
        "turn.failed",
        "thread.started",
        "assistant",
        "user",
        "result",
        "system",
        "error",
        "rate_limit_event",
    }:
        return None
    item = event.get("item")
    item_kind = item.get("type") if isinstance(item, dict) else None
    result: dict[str, Any] = {"event": kind, "digest": hashlib.sha256(line.encode()).hexdigest()}
    if item_kind in {
        "command_execution",
        "file_change",
        "web_search",
        "mcp_tool_call",
        "agent_message",
        "reasoning",
    }:
        result["activity"] = item_kind
    if isinstance(item, dict) and type(item.get("exit_code")) is int:
        result["exit_code"] = item["exit_code"]
    if isinstance(item, dict) and item.get("status") in {"in_progress", "completed", "failed"}:
        result["status"] = item["status"]
    return result


class Model(Protocol):
    def reconcile(self, run_directory: Path) -> None: ...

    def apply_authority(
        self, workspace: Path, authority: Authority, timeout: float, cancelled: Callable[[], bool]
    ) -> None: ...

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
    ) -> tuple[T, Usage]: ...

    def check(
        self,
        argv: tuple[str, ...],
        workspace: Path,
        timeout: float,
        cancelled: Callable[[], bool],
    ) -> tuple[bool, str]: ...


def run_process(
    argv: tuple[str, ...],
    workspace: Path,
    timeout: float,
    cancelled: Callable[[], bool],
    *,
    stdin: str = "",
    environment: dict[str, str] | None = None,
    supervision_seconds: float | None = None,
    observer: Callable[[float, int], None] | None = None,
    observation: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[int, str]:
    """Bound both streams while running and kill the owned group on every exit path."""
    env = {key: os.environ[key] for key in ("HOME", "PATH", "USER", "LANG") if key in os.environ}
    env.update(environment or {})
    with tempfile.TemporaryFile() as input_file:
        input_file.write(stdin.encode())
        input_file.seek(0)
        process = subprocess.Popen(
            (
                sys.executable,
                "-I",
                str(Path(__file__).with_name("process_guard.py")),
                str(os.getpid()),
                *argv,
            ),
            cwd=workspace,
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        output = bytearray()
        lines: dict[int, bytes] = {}
        observations = 0
        started = time.monotonic()
        supervised = started
        try:
            with selectors.DefaultSelector() as selector:
                assert process.stdout is not None and process.stderr is not None
                selector.register(process.stdout, selectors.EVENT_READ)
                selector.register(process.stderr, selectors.EVENT_READ)
                while selector.get_map():
                    if cancelled():
                        raise Stopped("RUN_STOPPED")
                    if time.monotonic() - started >= timeout:
                        raise TimeoutError("INVOCATION_TIMEOUT")
                    if (
                        supervision_seconds is not None
                        and time.monotonic() - supervised >= supervision_seconds
                    ):
                        supervised = time.monotonic()
                        if observer is not None:
                            observer(supervised - started, len(output))
                    for key, _ in selector.select(0.1):
                        data = os.read(key.fd, 65536)
                        if not data:
                            if quota_error(
                                lines.get(key.fd, b"").decode(errors="replace"),
                                stderr=key.fileobj is process.stderr,
                            ):
                                raise Stopped("USAGE_LIMIT")
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(data)
                        if len(output) > 8_000_000:
                            raise ValueError("TRANSPORT_OUTPUT_LIMIT")
                        pending = lines.get(key.fd, b"") + data
                        completed = pending.split(b"\n")
                        lines[key.fd] = completed.pop()
                        for line in completed:
                            if observation is not None and observations < 1000:
                                summary = summarize_event(line.decode(errors="replace"))
                                if summary is not None:
                                    observation(summary)
                                    observations += 1
                            if quota_error(
                                line.decode(errors="replace"), stderr=key.fileobj is process.stderr
                            ):
                                raise Stopped("USAGE_LIMIT")
                return process.wait(timeout=1), output.decode(errors="replace")
        finally:
            terminate_group(process)
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def codex_permissions(workspace: Path, authority: Authority) -> tuple[str, ...]:
    """No host-wide read grant: only minimal runtime files and the task workspace."""
    settings: dict[str, Any] = {
        "default_permissions": "fleet-worker",
        "permissions.fleet-worker.filesystem": {
            ":minimal": "read",
            str(workspace.resolve()): "write",
            str(workspace.resolve() / ".fleet-inputs"): "read",
            **{
                str(workspace.resolve() / name): "deny"
                for name in (".git", ".codex", ".claude", ".fleet", ".agents")
            },
        },
        "permissions.fleet-worker.network.enabled": bool(authority.network_hosts),
        "features.network_proxy": bool(authority.network_hosts),
        "permissions.fleet-worker.network.enable_socks5": False,
        "features.multi_agent": False,
        "features.shell_snapshot": False,
        "features.apps": False,
        "web_search": "disabled",
        "shell_environment_policy.inherit": "none",
        "shell_environment_policy.set": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    }
    if authority.network_hosts:
        port = 20000 + int(hashlib.sha256(str(workspace).encode()).hexdigest()[:8], 16) % 40000
        settings["permissions.fleet-worker.network.proxy_url"] = f"http://127.0.0.1:{port}"
        settings["permissions.fleet-worker.network.domains"] = {
            host: "allow" for host in authority.network_hosts
        }
    result: list[str] = []
    for key, value in settings.items():
        encoded = json.dumps(value)
        if isinstance(value, dict):
            encoded = (
                "{" + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in value.items()) + "}"
            )
        result.extend(("-c", key + "=" + encoded))
    return tuple(result)


def provider_schema(schema: type[Contract]) -> dict[str, Any]:
    result = schema.model_json_schema()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object":
                value["additionalProperties"] = False
                value["required"] = list(value.get("properties", {}))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    return result


def measured_tokens(raw: Any, *, claude: bool = False) -> int | None:
    if not isinstance(raw, dict):
        return None
    keys: tuple[str, ...] = ("input_tokens", "output_tokens")
    if claude:
        keys += ("cache_creation_input_tokens", "cache_read_input_tokens")
    if any(type(raw.get(key)) is not int or raw[key] < 0 for key in keys):
        return None
    return sum(int(raw[key]) for key in keys)


class NativeSandboxProbe:
    def __init__(self, codex: str = "codex", claude: str = "claude") -> None:
        self.executables = {"codex": shutil.which(codex), "claude": shutil.which(claude)}

    def _codex(self) -> str:
        executable = self.executables["codex"]
        if executable is None:
            raise ValueError("NATIVE_SANDBOX_UNAVAILABLE")
        return executable

    def preflight(
        self,
        workspace: Path,
        cancelled: Callable[[], bool],
        authority: Authority = _OFFLINE,
        timeout: float = 15,
    ) -> None:
        # No model access or operator credentials are required by this probe.
        with tempfile.TemporaryDirectory(prefix="fleet-probe-") as directory:
            denied = Path(directory) / "host-secret"
            denied.write_text("disposable probe")
            program = (
                "from pathlib import Path; "
                f"p=Path({str(workspace / ('.fleet-probe-' + secrets.token_hex(16)))!r}); "
                "p.open('x').close(); p.unlink(); "
                f"p=Path({str(denied)!r}); "
                "\ntry:\n p.read_text()\nexcept (PermissionError, FileNotFoundError):\n pass\n"
                "else:\n raise RuntimeError('host read not denied')\n"
            )
            argv = (
                self._codex(),
                *codex_permissions(workspace, authority),
                "sandbox",
                "--permission-profile",
                "fleet-worker",
                "--cd",
                str(workspace),
                "--",
                "/usr/bin/python3",
                "-I",
                "-c",
                program,
            )
            code, _ = run_process(argv, workspace, min(15, timeout), cancelled)
            if code:
                raise ValueError("NATIVE_SANDBOX_PREFLIGHT_FAILED")


def decode_response(output: str, schema: type[T]) -> tuple[T, Usage]:
    payload: Any = None
    usage = Usage()
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                try:
                    payload = json.loads(item["text"])
                except (ValueError, TypeError, KeyError):
                    # Native streams can contain ordinary commentary before
                    # the schema-constrained final agent message.
                    continue
        if event.get("type") == "turn.completed":
            usage = Usage(tokens=measured_tokens(event.get("usage")))
    return schema.model_validate(payload), usage
