"""Benchmark transport through the ordinary autonomous runtime and Candidate promotion.

Only the public workspace/checks enter the Run. The external grader remains
outside Fleet; a completed transport response is never a correctness claim.
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .candidates import Candidates
from .cli import projection
from .container import ContainerModel
from .engine import Engine
from .history import Journal, Stopped
from .models import Check, Contract, RunConfig, Text
from .native import Model


class Request(Contract):
    protocol: Literal["pocket-agent-v1"]
    operation: Literal["run"]
    control_dir: Path
    workspace: Path
    public_checks: Path
    instruction: Text
    seconds: float = Field(gt=0)
    writable_roots: tuple[Literal["src"], Literal["output"]] = ("src", "output")
    config: RunConfig


def checked_directory(path: Path, parent: Path | None = None) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("EXPLICIT_REGULAR_DIRECTORY_REQUIRED")
    path = path.resolve()
    if parent is not None and (not path.is_relative_to(parent) or path == parent):
        raise ValueError("BENCHMARK_PATH_ESCAPES_CONTROL_DIRECTORY")
    return path


def run(request: Request, model: Model | None = None) -> dict[str, Any]:
    control = checked_directory(request.control_dir)
    workspace = checked_directory(request.workspace, control)
    checks = checked_directory(request.public_checks, control)
    if workspace.is_relative_to(checks) or checks.is_relative_to(workspace):
        raise ValueError("BENCHMARK_INPUTS_OVERLAP")
    state = control / "fleet-autonomous"
    state.mkdir()  # Each invocation owns fresh state; no hidden legacy replay.
    candidates = Candidates(state / "objects")
    original_tree = candidates.capture(workspace)
    original = candidates.manifest(original_tree)
    if any(name.split("/")[0] not in {"input", "src", "output"} for name in original):
        raise ValueError("ONLY_PUBLIC_INPUT_ROOTS_ALLOWED")
    check_tree = candidates.capture(checks)
    check_files = candidates.manifest(check_tree)
    if set(check_files) != {"smoke.py", "execution.py"}:
        raise ValueError("EXPECTED_PUBLIC_SMOKE_AND_EXECUTION_CHECKS")
    # Check bytes are copied into operator-owned argv, never read back from a
    # worker-editable test file. execution.py is a public helper for smoke.py.
    sources = {
        name: (candidates.root / check_tree / str(entry["blob"])).read_text()
        for name, entry in check_files.items()
    }
    smoke = (
        "import sys,types; module=types.ModuleType('execution'); "
        "sys.modules['execution']=module; "
        f"exec(compile({sources['execution.py']!r},'execution.py','exec'),module.__dict__); "
        f"exec(compile({sources['smoke.py']!r},'smoke.py','exec'),{{'__name__':'__main__'}})"
    )
    public = Check(id="benchmark-public-smoke", argv=("/usr/bin/python3", "-I", "-B", "-c", smoke))
    if public.id in {item.id for item in request.config.checks}:
        raise ValueError("BENCHMARK_CHECK_ID_CONFLICT")
    config = RunConfig.model_validate(
        {
            **request.config.model_dump(mode="json"),
            "checks": [item.model_dump(mode="json") for item in (*request.config.checks, public)],
            "mandatory_checks": [*request.config.mandatory_checks, public.id],
            "limits": {
                **request.config.limits.model_dump(mode="json"),
                "wall_seconds": min(request.seconds, request.config.limits.wall_seconds),
            },
        }
    )
    journal = Journal(state / "history.db")
    engine = Engine(
        journal, candidates, model or ContainerModel(config.isolation), state / "workspaces"
    )
    run_id = engine.prepare(request.instruction, config, workspace)
    failure: str | None = None
    try:
        engine.execute(run_id)
    except (Stopped, ValueError, TimeoutError, OSError) as error:
        failure = str(error)[:200]
    view = projection(journal, run_id)
    events = journal.events(run_id)
    quota = any(
        event["kind"] == "stopped" and "USAGE_LIMIT" in event["body"]["reason"] for event in events
    )
    exported = False
    if view["status"] == "completed":
        destination = state / "published"
        engine.promote(run_id, destination)
        final_tree = candidates.capture(destination)
        final = candidates.manifest(final_tree)
        # The transport owns only src/output. It cannot accept changes to inputs
        # or newly introduced roots, even if a model said they were appropriate.
        if any(name.split("/")[0] not in {"input", "src", "output"} for name in final) or {
            name: entry for name, entry in original.items() if name.startswith("input/")
        } != {name: entry for name, entry in final.items() if name.startswith("input/")}:
            failure = "BENCHMARK_WRITE_CONTRACT_VIOLATION"
        elif candidates.capture(workspace) != original_tree:
            failure = "BENCHMARK_TARGET_CHANGED"
        else:
            for name in request.writable_roots:
                target = workspace / name
                if target.exists():
                    shutil.rmtree(target)
                if (destination / name).exists():
                    shutil.copytree(destination / name, target)
                else:
                    target.mkdir()
            exported = True
    settled = [event["body"]["usage"] for event in events if event["kind"] == "settled"]
    usage = {
        key: sum(item[key] for item in settled)
        if settled and all(item[key] is not None for item in settled)
        else None
        for key in ("tokens", "cost")
    }
    return {
        "protocol": request.protocol,
        "outcome": "usage_limit" if quota else "completed",
        "usage": usage,
        "details": {
            "run_id": run_id,
            "status": view["status"],
            "exported": exported,
            "failure": failure,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.request.is_symlink() or args.request.stat().st_size > 1_000_000:
        raise ValueError("INVALID_BENCHMARK_REQUEST")
    request = Request.model_validate_json(args.request.read_text())
    control = checked_directory(request.control_dir)
    if (
        args.response.parent.resolve() != control
        or args.response.exists()
        or args.response.is_symlink()
    ):
        raise ValueError("RESPONSE_MUST_BE_NEW_IN_CONTROL_DIRECTORY")
    response = run(request)
    with args.response.open("x") as stream:
        stream.write(json.dumps(response, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
