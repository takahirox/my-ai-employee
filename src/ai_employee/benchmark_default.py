"""Local CLI connection: ordinary Fleet defaults inside a disposable task container."""

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
from ai_employee.execution_profile import inspect_profile
from ai_employee.model_progress import ModelProgressRecord
from ai_employee.model_usage import inspect_usage
from ai_employee.storage import SQLiteStore


def execute(argv: list[str], *, cwd: Path, deadline: float, input: bytes | None = None) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("controller allowance exhausted")
    return subprocess.run(
        argv, cwd=cwd, input=input, capture_output=True, check=True, timeout=remaining
    ).stdout


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
    (root / ".fleet/project.json").write_text(json.dumps(harness))
    operator = home / "pocket/operator.json"
    configuration = {
        "schema_version": 1,
        "workers": {"codex_cli": {"executable": "/opt/pocket/guarded-codex"}},
    }
    # Validate while preserving the product's built-in routing defaults.
    OperatorConfig.model_validate(configuration)
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
            ],
            cwd=root,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
    if (logs / "usage-stop").exists():
        print("POCKET_USAGE_LIMIT", file=sys.stderr)
        return 75
    try:
        emitted = json.loads((logs / "fleet-result.json").read_text())
    except ValueError:
        print("Fleet did not return a structured outcome", file=sys.stderr)
        return 1
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
    if result.returncode == 0 and emitted.get("status") == "ready_to_promote":
        patch = execute(["fleet", "diff", emitted["run_id"]], cwd=root, deadline=deadline)
        # Export the captured parent candidate into this disposable task only.
        execute(["git", "apply", "--check", "-"], input=patch, cwd=root, deadline=deadline)
        execute(["git", "apply", "-"], input=patch, cwd=root, deadline=deadline)
    # Returned unsuccessful attempts are graded on their actual artifacts.
    return 0


def main() -> int:
    request = json.loads(Path(sys.argv[1]).read_text())
    return run(request, Path("/app"), Path("/home/agent"), Path("/logs/agent"))


if __name__ == "__main__":
    raise SystemExit(main())
