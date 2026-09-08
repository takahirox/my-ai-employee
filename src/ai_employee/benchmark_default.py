"""Local CLI connection with adaptive routing, candidate validation and bounded diagnostics."""

import argparse
import json
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ai_employee.benchmark_adapter import make_harness
from ai_employee.config import OperatorConfig
from ai_employee.domain.v2 import WorkerBoundaryDiagnostic
from ai_employee.execution_profile import inspect_profile
from ai_employee.goal_acceptance import GoalChecks
from ai_employee.model_progress import ModelProgressRecord
from ai_employee.model_usage import ModelProcessDiagnostic, inspect_usage
from ai_employee.plan_review import PlanReviewFailureEvidence
from ai_employee.storage import SQLiteStore
from ai_employee.worker_observation import exact_hosts


def execute(argv: list[str], *, cwd: Path, deadline: float, input: bytes | None = None) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("controller allowance exhausted")
    return subprocess.run(
        argv, cwd=cwd, input=input, capture_output=True, check=True, timeout=remaining
    ).stdout


def configure_validation(request: dict[str, Any], harness: dict[str, Any], root: Path) -> list[str]:
    """Bind operator-supplied public checks; never infer authority or read hidden grading data."""
    settings = request.get("settings", {})
    if not isinstance(settings, dict) or set(settings) - {"observation_hosts", "public_acceptance"}:
        raise ValueError("unknown public validation settings")
    declared_hosts = settings.get("observation_hosts", [])
    if not isinstance(declared_hosts, list) or any(
        not isinstance(host, str) for host in declared_hosts
    ):
        raise ValueError("observation hosts must be a list of exact host strings")
    hosts = exact_hosts(tuple(declared_hosts))
    harness["worker"].update(scratch_validation=True, observation_hosts=list(hosts))
    # Existing parent review evaluates the captured candidate against the original
    # request, including requirements that structural smoke cannot establish.
    harness["verification"]["review"] = {"required": True, "parent_semantic_review": True}
    acceptance = settings.get("public_acceptance")
    if acceptance is None:
        return []
    if not isinstance(acceptance, dict) or set(acceptance) != {"checks", "commands"}:
        raise ValueError("public acceptance requires explicit checks and commands")
    checks = GoalChecks.model_validate_json(json.dumps(acceptance["checks"]))
    if checks.goal != request["instruction"]:
        raise ValueError("public acceptance must bind the exact original instruction")
    commands = acceptance["commands"]
    if not isinstance(commands, dict) or set(commands) != {c.command_ref for c in checks.criteria}:
        raise ValueError("public acceptance commands must match criterion references")
    for name, code in commands.items():
        if name in harness["commands"] or not isinstance(code, str) or len(code) > 16_000:
            raise ValueError("public acceptance command conflicts or exceeds the bound")
        harness["commands"][name] = {"argv": ["python", "-I", "-c", code], "cwd": "."}
    target = root / ".fleet/public-acceptance.json"
    target.write_text(checks.model_dump_json())
    return ["--acceptance-file", str(target)]


def run(request: dict[str, Any], root: Path, home: Path, logs: Path) -> int:
    seconds = request["seconds"]
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("positive finite remaining allowance required")
    deadline = time.monotonic() + seconds
    logs.mkdir(parents=True, exist_ok=True)
    harness = make_harness(request["seconds"])
    # Transport paths/checks are unchanged; remove the legacy fixed-worker restriction.
    routing = OperatorConfig().routing
    assert routing is not None and routing.default_strategy_set is not None
    harness["worker"] = {
        "allowed": ["codex_cli"],
        "adaptive_routing": True,
        "allowed_strategy_ids": list(routing.strategy_sets[routing.default_strategy_set]),
    }
    harness["budgets"] = {"wall_seconds": request["seconds"]}
    checks = root / ".fleet/public-checks"
    checks.mkdir(parents=True)
    for name in ("smoke.py", "execution.py"):
        shutil.copyfile(Path("/opt/pocket") / name, checks / name)
    acceptance_args = configure_validation(request, harness, root)
    (root / ".fleet/project.json").write_text(json.dumps(harness))
    operator = home / "pocket/operator.json"
    configuration = {
        "schema_version": 1,
        "workers": {"codex_cli": {"executable": "/opt/pocket/guarded-codex"}},
        "worker_observation_hosts": harness["worker"]["observation_hosts"],
        "routing": routing.model_dump(mode="json"),
    }
    # Validate while preserving the product's built-in routing defaults.
    configuration["routing"]["default_parent_reviewer_strategy"] = "codex-sol-high"
    for strategy in configuration["routing"]["strategies"]:
        if strategy["id"] == "codex-sol-high":
            strategy["parent_reviewer_eligible"] = True
    OperatorConfig.model_validate_json(json.dumps(configuration))
    operator.write_text(json.dumps(configuration))
    # Setup and accepted-patch export share the request's remaining allowance.
    remaining = deadline - time.monotonic() - min(2.0, seconds / 20)
    if remaining <= 0:
        raise TimeoutError("setup exhausted the controller allowance")
    harness["budgets"]["wall_seconds"] = remaining
    (root / ".fleet/project.json").write_text(json.dumps(harness))
    execute(["git", "init", "-q"], cwd=root, deadline=deadline)
    execute(["git", "add", "."], cwd=root, deadline=deadline)
    execute(
        [
            "git",
            "-c",
            "user.name=Benchmark",
            "-c",
            "user.email=bench@example.invalid",
            "commit",
            "-qm",
            "public fixture",
        ],
        cwd=root,
        deadline=deadline,
    )
    with (
        (logs / "fleet-result.json").open("w") as stdout,
        (logs / "fleet.stderr").open("w") as stderr,
    ):
        result = subprocess.run(
            [
                "fleet",
                "work",
                request["instruction"],
                "--repo",
                str(root),
                "--operator-config",
                str(operator),
                "--non-interactive",
                "--json",
                *acceptance_args,
            ],
            cwd=root,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
    quota_stopped = (logs / "usage-stop").exists()
    try:
        emitted = json.loads((logs / "fleet-result.json").read_text())
    except ValueError:
        print(
            "POCKET_USAGE_LIMIT" if quota_stopped else "Fleet did not return a structured outcome",
            file=sys.stderr,
        )
        return 75 if quota_stopped else 1
    print(
        json.dumps(
            {
                "type": "fleet.outcome",
                "status": emitted.get("status"),
                "stable_code": emitted.get("stable_code"),
                "exit_code": result.returncode,
            }
        )
    )
    usage_complete = False
    if emitted.get("run_id"):
        with SQLiteStore(home / ".fleet/fleet.db") as store:
            usage = inspect_usage(store, [emitted["run_id"]])
            details = {
                "model_process_diagnostics": [
                    record.model_dump(mode="json")
                    for record in store.list_records(
                        "model_process_diagnostic_v2",
                        ModelProcessDiagnostic,
                        run_id=emitted["run_id"],
                    )
                ],
                "worker_boundary_diagnostics": [
                    record.model_dump(mode="json", exclude={"exception_message"})
                    for record in store.list_records(
                        "worker_boundary_diagnostic_v2",
                        WorkerBoundaryDiagnostic,
                        run_id=emitted["run_id"],
                    )
                ],
                "plan_review_failures": [
                    record.model_dump(mode="json")
                    for record in store.list_records(
                        "plan_review_failure_evidence_v2",
                        PlanReviewFailureEvidence,
                        run_id=emitted["run_id"],
                    )
                ],
                "usage": usage,
                "profile": inspect_profile(store, emitted["run_id"]),
                "progress": [
                    record.model_dump(mode="json")
                    for record in store.list_records(
                        "model_progress_v2", ModelProgressRecord, run_id=emitted["run_id"]
                    )
                ],
            }
        (logs / "fleet-diagnostics.json").write_text(json.dumps(details, indent=2))
        invocations = usage["invocation_details"]
        usage_complete = (
            isinstance(invocations, list)
            and bool(invocations)
            and all(
                isinstance(invocation, dict) and invocation.get("complete") is True
                for invocation in invocations
            )
        )
    # Numeric records come from the native transport; this event adds no tokens.
    print(json.dumps({"type": "pocket.usage", "usage": {}, "complete": usage_complete}))
    if quota_stopped:
        print("POCKET_USAGE_LIMIT", file=sys.stderr)
        return 75
    if result.returncode == 0 and emitted.get("status") == "ready_to_promote":
        patch = execute(["fleet", "diff", emitted["run_id"]], cwd=root, deadline=deadline)
        # Export the captured parent candidate into this disposable task only.
        execute(["git", "apply", "--check", "-"], input=patch, cwd=root, deadline=deadline)
        execute(["git", "apply", "-"], input=patch, cwd=root, deadline=deadline)
    # Returned unsuccessful attempts are graded on their actual artifacts.
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument(
        "--settings", type=Path, help="trusted public observation/acceptance settings"
    )
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    if args.settings is not None:
        if (
            not args.settings.is_file()
            or args.settings.is_symlink()
            or args.settings.stat().st_size > 64_000
        ):
            raise ValueError("public settings must be a bounded regular file")
        if "settings" in request:
            raise ValueError("supply settings through either the request or the CLI, not both")
        request["settings"] = json.loads(args.settings.read_text())
    return run(request, Path("/app"), Path("/home/agent"), Path("/logs/agent"))


if __name__ == "__main__":
    raise SystemExit(main())
