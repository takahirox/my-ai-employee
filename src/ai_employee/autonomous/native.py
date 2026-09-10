"""Native CLI sessions; Fleet supervises processes rather than individual actions."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol, TypeVar

from .history import Stopped
from .models import Authority, Contract, StagePolicy, Usage

T = TypeVar("T", bound=Contract)


class Model(Protocol):
    def apply_authority(self, workspace: Path, authority: Authority) -> None: ...

    def generate(
        self,
        policy: StagePolicy,
        prompt: str,
        schema: type[T],
        workspace: Path,
        authority: Authority,
        timeout: float,
        cancelled: Callable[[], bool],
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
) -> tuple[int, str]:
    """Bound both streams while running and kill the owned group on every exit path."""
    env = {key: os.environ[key] for key in ("HOME", "PATH", "USER", "LANG") if key in os.environ}
    env.update(environment or {})
    with tempfile.TemporaryFile() as input_file:
        input_file.write(stdin.encode())
        input_file.seek(0)
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        output = bytearray()
        started = time.monotonic()
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
                    for key, _ in selector.select(0.1):
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(data)
                        if len(output) > 8_000_000:
                            raise ValueError("TRANSPORT_OUTPUT_LIMIT")
                        # Stop immediately, before retry/escalation or another model call.
                        tail = output[-131072:].decode(errors="replace").lower()
                        if any(
                            marker in tail
                            for marker in (
                                "usage_limit_reached",
                                "usage limit reached",
                                "hit your usage limit",
                                "insufficient_quota",
                                "credit balance is too low",
                            )
                        ):
                            raise Stopped("USAGE_LIMIT")
                return process.wait(timeout=1), output.decode(errors="replace")
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
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


class NativeModel:
    def __init__(self, codex: str = "codex", claude: str = "claude") -> None:
        self.executables = {"codex": shutil.which(codex), "claude": shutil.which(claude)}

    def _codex(self) -> str:
        executable = self.executables["codex"]
        if executable is None:
            raise ValueError("NATIVE_SANDBOX_UNAVAILABLE")
        return executable

    def preflight(self, workspace: Path, cancelled: Callable[[], bool]) -> None:
        # No model access or operator credentials are required by this probe.
        with tempfile.TemporaryDirectory(prefix="fleet-probe-") as directory:
            denied = Path(directory) / "host-secret"
            denied.write_text("disposable probe")
            program = (
                "from pathlib import Path; "
                f"p=Path({str(workspace / 'probe')!r}); p.write_text('ok'); p.unlink(); "
                f"p=Path({str(denied)!r}); "
                "\ntry:\n p.read_text()\nexcept PermissionError:\n pass\n"
                "else:\n raise RuntimeError('host read not denied')\n"
            )
            argv = (
                self._codex(),
                *codex_permissions(workspace, Authority()),
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
            code, _ = run_process(argv, workspace, 15, cancelled)
            if code:
                raise ValueError("NATIVE_SANDBOX_PREFLIGHT_FAILED")

    def apply_authority(self, workspace: Path, authority: Authority) -> None:
        if authority.credentials or authority.operation_approval or authority.duplicate_prevention:
            raise ValueError("REQUIRED_AUTHORITY_BOUNDARY_UNAVAILABLE")
        self.preflight(workspace, lambda: False)

    def generate(
        self,
        policy: StagePolicy,
        prompt: str,
        schema: type[T],
        workspace: Path,
        authority: Authority,
        timeout: float,
        cancelled: Callable[[], bool],
    ) -> tuple[T, Usage]:
        if authority.credentials or authority.operation_approval or authority.duplicate_prevention:
            raise ValueError("REQUIRED_AUTHORITY_BOUNDARY_UNAVAILABLE")
        executable = self.executables[policy.backend]
        if executable is None:
            raise ValueError("WORKER_UNAVAILABLE")
        self.preflight(workspace, cancelled)
        with tempfile.TemporaryDirectory(prefix="fleet-transport-") as directory:
            schema_path = Path(directory) / "schema.json"
            schema_path.write_text(json.dumps(provider_schema(schema)))
            if policy.backend == "codex":
                argv = (
                    executable,
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
                    f'model_reasoning_effort="{policy.effort}"',
                    *codex_permissions(workspace, authority),
                    "--cd",
                    str(workspace),
                    "--output-schema",
                    str(schema_path),
                    "-",
                )
            else:
                settings = {
                    "sandbox": {
                        "enabled": True,
                        "failIfUnavailable": True,
                        "allowUnsandboxedCommands": False,
                        "autoAllowBashIfSandboxed": True,
                        "enableWeakerNestedSandbox": False,
                        "filesystem": {
                            "denyRead": ["/"],
                            "allowRead": [
                                "/usr",
                                "/bin",
                                "/lib",
                                "/lib64",
                                "/System",
                                "/dev",
                                str(workspace),
                            ],
                            "allowWrite": [str(workspace)],
                        },
                        "network": {
                            "allowedDomains": list(authority.network_hosts),
                            "strictAllowlist": True,
                        },
                    },
                    "permissions": {"defaultMode": "acceptEdits"},
                }
                argv = (
                    executable,
                    "--print",
                    "--output-format",
                    "json",
                    "--safe-mode",
                    "--restricted",
                    "--strict-mcp-config",
                    "--mcp-config",
                    '{"mcpServers":{}}',
                    "--setting-sources",
                    "",
                    "--settings",
                    json.dumps(settings),
                    "--tools",
                    "Bash,Read,Glob,Grep,Edit,Write",
                    "--no-session-persistence",
                    "--model",
                    policy.model,
                    "--effort",
                    policy.effort,
                    "--json-schema",
                    schema_path.read_text(),
                )
            code, output = run_process(argv, workspace, timeout, cancelled, stdin=prompt)
        if code:
            raise ValueError("WORKER_PROCESS_FAILED")
        payload: Any = None
        usage = Usage()
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if policy.backend == "claude" and event.get("type") == "result":
                if event.get("is_error"):
                    raise ValueError("WORKER_PROCESS_FAILED")
                payload = event.get("structured_output")
                raw_usage = event.get("usage", {})
                usage = Usage(
                    tokens=sum(
                        raw_usage.get(k, 0)
                        for k in (
                            "input_tokens",
                            "output_tokens",
                            "cache_creation_input_tokens",
                            "cache_read_input_tokens",
                        )
                    )
                    if raw_usage
                    else None,
                    cost=event.get("total_cost_usd"),
                )
            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    payload = json.loads(item["text"])
            if event.get("type") == "turn.completed":
                raw_usage = event.get("usage", {})
                usage = Usage(
                    tokens=raw_usage.get("input_tokens", 0) + raw_usage.get("output_tokens", 0)
                    if raw_usage
                    else None
                )
        return schema.model_validate(payload), usage

    def check(
        self,
        argv: tuple[str, ...],
        workspace: Path,
        timeout: float,
        cancelled: Callable[[], bool],
    ) -> tuple[bool, str]:
        command = (
            self._codex(),
            *codex_permissions(workspace, Authority()),
            "sandbox",
            "--permission-profile",
            "fleet-worker",
            "--cd",
            str(workspace),
            "--",
            *argv,
        )
        code, output = run_process(command, workspace, timeout, cancelled)
        # Caller records a digest, not arbitrary output/secret-bearing trace bodies.
        import hashlib

        return code == 0, hashlib.sha256(output.encode()).hexdigest()
