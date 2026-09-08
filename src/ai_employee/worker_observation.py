"""Explicit, exact-host native observation in a disposable candidate copy."""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import tempfile
from pathlib import Path


def exact_hosts(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) > 32 or len(values) != len(set(values)):
        raise ValueError("observation hosts must be unique and bounded")
    for host in values:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host) or ".." in host:
            raise ValueError(
                "observation requires exact lowercase hosts; no URLs, ports or wildcards"
            )
    return values


def observation_authority(
    requested: tuple[str, ...], authorized: tuple[str, ...]
) -> tuple[str, ...]:
    exact_hosts(requested)
    exact_hosts(authorized)
    if not set(requested) <= set(authorized):
        raise ValueError("WORKER_OBSERVATION_UNAUTHORIZED: Harness hosts exceed operator allowlist")
    return requested


def prepare_scratch(repository: Path, parent: Path, *, candidate: bool = False) -> Path:
    """Export only tracked public candidate files, excluding host Git and untracked secrets."""
    from .isolated_worker import candidate_archive

    data = candidate_archive(repository, 16_000_000, include_untracked=candidate)
    parent.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix="candidate-", dir=parent))
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(target, filter="data")
    (target / ".tmp").mkdir(exist_ok=True)
    return target


def observation_proxy_url(scratch: str) -> str:
    port = 20_000 + int(hashlib.sha256(scratch.encode()).hexdigest()[:8], 16) % 40_000
    return f"http://127.0.0.1:{port}"


def observation_args(
    scratch: str, hosts: tuple[str, ...], repository: str | None = None
) -> tuple[str, ...]:
    exact_hosts(hosts)
    settings = {
        "default_permissions": "fleet-observe",
        "permissions.fleet-observe.filesystem": {
            ":root": "read",
            str(Path(scratch)): "write",
            str(Path(scratch) / ".git"): "read",
            str(Path(scratch) / ".codex"): "read",
            str(Path(scratch) / ".fleet"): "read",
            str(Path(scratch) / "input"): "read",
        },
        "permissions.fleet-observe.network.enabled": bool(hosts),
        "features.network_proxy": bool(hosts),
        "features.multi_agent": False,
        "features.shell_snapshot": False,
        "web_search": "disabled",
        "shell_environment_policy.set": {"TMPDIR": str(Path(scratch) / ".tmp")},
    }
    filesystem = settings["permissions.fleet-observe.filesystem"]
    assert isinstance(filesystem, dict)
    filesystem[str(Path.home() / ".codex")] = "deny"
    if repository is not None:
        filesystem[str(Path(repository).resolve())] = "read"
    args: list[str] = []
    for key, value in settings.items():
        if isinstance(value, dict):
            encoded = (
                "{" + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in value.items()) + "}"
            )
        else:
            encoded = json.dumps(value)
        args.extend(("-c", f"{key}={encoded}"))
    if hosts:
        # Independent workers must not contend for Codex's fixed default listeners.
        # A bind race fails closed during native startup; it never broadens access.
        args.extend(
            (
                "-c",
                f'permissions.fleet-observe.network.proxy_url="{observation_proxy_url(scratch)}"',
                "-c",
                "permissions.fleet-observe.network.enable_socks5=false",
            )
        )
        domains = "{" + ",".join(json.dumps(host) + '="allow"' for host in hosts) + "}"
        args.extend(("-c", f"permissions.fleet-observe.network.domains={domains}"))
    return tuple(args)
