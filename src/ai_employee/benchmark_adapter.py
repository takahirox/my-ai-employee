"""Product-owned pocket-agent-v1 controller. No dependency on benchmark internals.

This trusted host controller invokes the ordinary Fleet work orchestration with an
explicit private state store. All model tools and checks use the production isolated
Docker executor. Only public inputs and a public structural check are supplied.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import cli
from .domain.v2 import WorkerResult
from .isolated_worker import IsolatedWorkerProfile
from .serialization import canonical_json
from .storage import SQLiteStore

PROTOCOL = "pocket-agent-v1"


def checked_directory(value: str, parent: Path | None = None) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("explicit regular controller directory required")
    path = path.resolve()
    if parent and (not path.is_relative_to(parent) or path == parent):
        raise ValueError("controller path escapes its private control directory")
    return path


def cleanup(control: Path) -> None:
    ledger = control / "resources.jsonl"
    if not ledger.exists():
        return
    if ledger.is_symlink() or ledger.stat().st_size > 100_000:
        raise ValueError("invalid resource ledger")
    resources = [json.loads(line) for line in ledger.read_text().splitlines()]
    latest = {}
    for item in resources:
        pattern = r"fleet-candidate-[0-9a-f]{32}" + (
            r"-network" if item.get("kind") == "network" else r"(?:-proxy)?"
        )
        if item.get("kind") not in ("network", "container") or not re.fullmatch(
            pattern, item.get("name", "")
        ):
            raise ValueError("invalid owned resource name")
        if item.get("state", "created") not in ("intent", "created"):
            raise ValueError("invalid resource lifecycle state")
        latest[(item["kind"], item["name"])] = item
    # Containers first, then their networks. Never enumerate or prune other workloads.
    for item in sorted(latest.values(), key=lambda r: r["kind"] == "network"):
        args = ["docker", item["kind"], "rm"]
        if item["kind"] == "container":
            args.append("-f")
        result = subprocess.run([*args, item["name"]], capture_output=True, timeout=15)
        error = result.stderr.decode(errors="replace").lower()
        missing = f"no such {item['kind']}: {item['name']}" in error or (
            item["kind"] == "network" and f"network {item['name']} not found" in error
        )
        if result.returncode and not missing:
            raise RuntimeError("Owned benchmark resource cleanup was not confirmed")
    if any(item.get("state") == "intent" for item in latest.values()):
        # A killed Docker client may leave a create request in flight in the daemon.
        # Absence at one instant is not sufficient evidence of completed cleanup.
        raise RuntimeError("Resource creation was interrupted; retain ledger for operator recovery")


def make_harness(seconds: float) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "commands": {
            "smoke": {
                "argv": [
                    "python",
                    "-I",
                    "-B",
                    "-c",
                    "import runpy; "
                    "runpy.run_path('.fleet/public-checks/smoke.py',run_name='__main__')",
                ],
                "cwd": ".",
            }
        },
        "paths": {
            "writable": ["src/**", "output/**"],
            "protected": ["input/**", ".git/**", ".fleet/**"],
        },
        "verification": {"required": ["smoke"], "review": {"required": False}},
        "worker": {
            "allowed": ["codex_cli"],
            "allowed_strategy_ids": ["benchmark"],
            "adaptive_routing": False,
            "isolated_workspace_tools": True,
        },
        "budgets": {"wall_seconds": seconds, "processes": 600, "worker_turns": 1},
    }


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args], input=data, capture_output=True, timeout=15
    )
    if result.returncode:
        raise RuntimeError("Disposable benchmark Git operation failed: " + args[0])
    return result.stdout


def regular_files(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("benchmark files must not be symlinks")
        if path.is_dir():
            continue
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError("benchmark files must be regular")
        total += path.stat().st_size
        if total > 16 * 1024 * 1024 or len(result) >= 4096:
            raise ValueError("benchmark workspace exceeds transport budget")
        result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def run(request: dict[str, Any], control: Path) -> dict[str, Any]:
    workspace = checked_directory(request["workspace"], control)
    checks = checked_directory(request["public_checks"], control)
    seconds = request["seconds"]
    if type(seconds) not in (float, int) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("finite positive wall budget required")
    if request.get("writable_roots") != ["src", "output"]:
        raise ValueError("unsupported workspace write contract")
    if not request.get("model") or not request.get("effort"):
        raise ValueError("explicit model and effort required; no provider/model fallback")
    settings = request["settings"]
    profile = IsolatedWorkerProfile.model_validate(
        {
            **settings["isolated_worker"],
            "resource_ledger": str(control / "resources.jsonl"),
        }
    )
    if not profile.auth_file:
        raise ValueError("explicit delegated authentication required")
    initial = regular_files(workspace)
    if any(name.split("/")[0] not in ("input", "src", "output") for name in initial):
        raise ValueError("only public task roots may enter the controller")
    root = control / "candidate"
    root.mkdir()
    for name, data in initial.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for name in ("input", "src", "output"):
        (root / name).mkdir(exist_ok=True)
    public_checks = regular_files(checks)
    if set(public_checks) != {"smoke.py", "execution.py"}:
        raise ValueError("only the benchmark public smoke/execution checks are accepted")
    (root / ".fleet/public-checks").mkdir(parents=True)
    for name, data in public_checks.items():
        (root / ".fleet/public-checks" / name).write_bytes(data)
    (root / ".fleet/project.json").write_text(json.dumps(make_harness(float(seconds))))
    git(root, "init", "-q")
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Benchmark",
        "-c",
        "user.email=bench@example.invalid",
        "commit",
        "-qm",
        "public fixture",
    )
    operator = control / "operator.json"
    operator.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "isolated_worker": profile.model_dump(mode="json"),
                "workers": {"codex_cli": {"executable": "codex"}},
                "routing": {
                    "strategies": [
                        {
                            "id": "benchmark",
                            "backend": "codex_cli",
                            "model": request["model"],
                            "effort": request["effort"],
                            "capabilities": ["edit_intent", "process"],
                        }
                    ]
                },
            }
        )
    )
    operator.chmod(0o600)
    database = control / "state/fleet.db"
    database.parent.mkdir()
    args = cli.build_parser().parse_args(
        [
            "work",
            request["instruction"],
            "--repo",
            str(root),
            "--operator-config",
            str(operator),
            "--routing-mode",
            "fixed",
            "--strategy",
            "benchmark",
            "--non-interactive",
            "--json",
        ]
    )
    # The normal CLI intentionally pins its user DB. Reuse its production work
    # orchestration with an explicit benchmark-owned DB, not an environment override
    # or monkeypatch. Runtime/executor/verification/policy code is unchanged.
    args.db = str(database)
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = cli._work(args)
    emitted = json.loads(stdout.getvalue())
    with SQLiteStore(database) as store:
        workers = store.list_records("worker_result_v2", WorkerResult)
        quota = any(w.failure and "USAGE_LIMIT" in w.failure.message for w in workers)
        usage_rows = [dict(w.usage) for w in workers if isinstance(w.usage, Mapping)]
        usage = {
            key: sum(row[key] for row in usage_rows)
            for key in ("input_tokens", "cached_input_tokens", "output_tokens")
            if usage_rows and all(type(row.get(key)) is int for row in usage_rows)
        }
        details = {
            "status": emitted.get("status"),
            "stable_code": emitted.get("stable_code"),
            "exit_code": exit_code,
            "worker_results": len(workers),
            "runtime_image": profile.image,
            "failures": [w.failure.code.value for w in workers if w.failure],
            "resource_usage": [w.resource_usage for w in workers if w.resource_usage],
            "human_active_seconds": None,
            "human_interventions": 0,
            "implementation_sha256": hashlib.sha256(
                b"".join(
                    path.relative_to(Path(__file__).parent).as_posix().encode()
                    + b"\0"
                    + path.read_bytes()
                    for path in sorted(Path(__file__).parent.rglob("*.py"))
                )
            ).hexdigest(),
        }
        if quota:
            return {
                "protocol": PROTOCOL,
                "outcome": "usage_limit",
                "usage": usage,
                "details": details,
            }
        if exit_code != 0 or emitted.get("status") != "ready_to_promote":
            # A normally returned but unsuccessful attempt is still submitted to
            # the unchanged external grader. "completed" never means "correct".
            return {
                "protocol": PROTOCOL,
                "outcome": "completed",
                "usage": usage,
                "details": details,
            }
        patch_output = io.StringIO()
        with contextlib.redirect_stdout(patch_output):
            cli._diff(store, argparse.Namespace(run_id=emitted["run_id"], stat=False))
    patch = patch_output.getvalue().encode()
    if len(patch) > 16 * 1024 * 1024:
        raise ValueError("candidate patch exceeds transport budget")
    git(root, "apply", "--check", "-", data=patch)
    git(root, "apply", "-", data=patch)
    # Fleet has accepted paths and verified this captured patch independently.
    # Materialize only src/output in the disposable public snapshot, never user source.
    for name in ("src", "output"):
        files = regular_files(root / name)
        destination = workspace / name
        if destination.exists():
            regular_files(destination)
            shutil.rmtree(destination)
        destination.mkdir()
        for relative, data in files.items():
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    return {"protocol": PROTOCOL, "outcome": "completed", "usage": usage, "details": details}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.request.is_symlink() or args.request.stat().st_size > 1_000_000:
        raise ValueError("invalid controller request")
    request = json.loads(args.request.read_text())
    if request.get("protocol") != PROTOCOL:
        raise ValueError("unsupported benchmark protocol")
    control = checked_directory(request["control_dir"])
    if args.response.parent.resolve() != control or args.response.exists():
        raise ValueError("response must be a new file in the private control directory")
    response: dict[str, Any]
    if request.get("operation") == "cleanup":
        cleanup(control)
        response = {"protocol": PROTOCOL, "outcome": "cleaned"}
    elif request.get("operation") == "run":
        try:
            response = run(request, control)
        except Exception as error:
            response = {
                "protocol": PROTOCOL,
                "outcome": "failed",
                "details": {"error_type": type(error).__name__},
            }
        finally:
            cleanup(control)
    else:
        raise ValueError("unsupported benchmark operation")
    with args.response.open("x") as stream:
        stream.write(canonical_json(response) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
