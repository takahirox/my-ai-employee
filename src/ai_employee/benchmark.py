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
    operation: Literal["run", "cleanup"]
    control_dir: Path
    workspace: Path
    public_checks: Path
    instruction: Text
    seconds: float = Field(ge=0)
    writable_roots: tuple[Literal["src"], Literal["output"]] = ("src", "output")
    config: RunConfig | None = None
    model: Text | None = None
    effort: Text | None = None
    settings: dict[str, Any] = Field(default_factory=dict)

    def configured(self) -> RunConfig:
        if self.config is not None and self.settings:
            raise ValueError("AMBIGUOUS_BENCHMARK_CONFIGURATION")
        if set(self.settings) - {"config"}:
            raise ValueError("UNSUPPORTED_BENCHMARK_SETTINGS")
        selected = self.config or RunConfig.model_validate(self.settings.get("config"))
        data = selected.model_dump(mode="json")

        # Experimental model/effort are fixed across all model calls, including
        # configured reviewers and recovery. Never infer a different provider.
        def override(policy: dict[str, Any]) -> None:
            if self.model is not None:
                policy["model"] = self.model
                policy["reviewer_model"] = self.model
            if self.effort is not None:
                policy["effort"] = self.effort
                policy["reviewer_effort"] = self.effort

        for key in ("clarification", "planning", "worker", "verification", "recovery", "selection"):
            if data[key] is not None:
                override(data[key])
        for key in ("worker_options", "worker_escalations"):
            for policy in data[key]:
                override(policy)
        return RunConfig.model_validate(data)


def public_check(sources: dict[str, str]) -> Check:
    # Execute exact public artifacts with their real module/script context. No
    # implementation script is run by this structural check.
    program = (
        "import tempfile,runpy; from pathlib import Path; "
        f"sources={sources!r}; "
        "\nwith tempfile.TemporaryDirectory(prefix='fleet-public-') as directory:\n"
        " root=Path(directory)\n"
        " for name, source in sources.items(): (root/name).write_text(source)\n"
        " runpy.run_path(str(root/'smoke.py'),run_name='__main__')\n"
    )
    return Check(id="benchmark-public-smoke", argv=("/usr/bin/python3", "-I", "-B", "-c", program))


def cleanup(request: Request, model: Model | None = None) -> dict[str, Any]:
    control = checked_directory(request.control_dir)
    state = control / "fleet-autonomous"
    if state.is_symlink():
        raise ValueError("INVALID_BENCHMARK_STATE")
    if state.exists():
        workspaces = state / "workspaces"
        if workspaces.is_symlink():
            raise ValueError("INVALID_BENCHMARK_STATE")
        journal = Journal(state / "history.db")
        for run_id in journal.runs():
            journal.stop(run_id, "BENCHMARK_CLEANUP")
            with journal.controller(run_id):
                configured = journal.config(run_id)
                adapter = model or ContainerModel(configured.isolation)
                adapter.reconcile(state / "workspaces" / run_id)
    return {
        "protocol": request.protocol,
        "outcome": "cleaned",
        "details": {"cleanup": "confirmed", "history_retained": state.exists()},
    }


def checked_directory(path: Path, parent: Path | None = None) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("EXPLICIT_REGULAR_DIRECTORY_REQUIRED")
    path = path.resolve()
    if parent is not None and (not path.is_relative_to(parent) or path == parent):
        raise ValueError("BENCHMARK_PATH_ESCAPES_CONTROL_DIRECTORY")
    return path


def run(request: Request, model: Model | None = None) -> dict[str, Any]:
    if request.operation == "cleanup":
        return cleanup(request, model)
    if request.seconds <= 0:
        return {
            "protocol": request.protocol,
            "outcome": "failed",
            "details": {"failure": "BENCHMARK_BUDGET_EXHAUSTED"},
        }
    configured = request.configured()
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
    public = public_check(sources)
    if public.id in {item.id for item in configured.checks}:
        raise ValueError("BENCHMARK_CHECK_ID_CONFLICT")
    config = RunConfig.model_validate(
        {
            **configured.model_dump(mode="json"),
            "checks": [item.model_dump(mode="json") for item in (*configured.checks, public)],
            "mandatory_checks": [*configured.mandatory_checks, public.id],
            "limits": {
                **configured.limits.model_dump(mode="json"),
                "wall_seconds": min(request.seconds, configured.limits.wall_seconds),
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
    except (Stopped, ValueError, TimeoutError, OSError, RuntimeError) as error:
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
