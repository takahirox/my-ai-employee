"""Explicitly opted-in, real-model paired experiment; never part of ordinary CI.

Both arms use the real CLI orchestration and one immutable Docker runtime. Only
the proposal arm's process transport is replaced, to keep its existing adapter
and wire format while moving execution and verification off the host.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from ai_employee import cli
from ai_employee.domain.base import freeze_json
from ai_employee.domain.v2 import ExecutionResult, StableFailure, StableFailureCode, WorkerResult
from ai_employee.graph_evaluation import ParentCandidateEvaluationRecord
from ai_employee.isolated_execution import (
    DockerProcessExecutor,
    _reports_usage_limit,
    codex_isolated_permission_args,
)
from ai_employee.isolated_worker import DockerCandidate, IsolatedWorkerProfile
from ai_employee.serialization import canonical_json
from ai_employee.services_v2._common import identifier, now
from ai_employee.services_v2.process import LocalProcessExecutor
from ai_employee.storage import SQLiteStore

IMAGE = os.environ.get("FLEET_LIVE_IMAGE")
AUTH = os.environ.get("FLEET_LIVE_AUTH_FILE")
MODEL = os.environ.get("FLEET_LIVE_MODEL")
OUTPUT = os.environ.get("FLEET_LIVE_OUTPUT")
live_test = pytest.mark.skipif(
    not all((IMAGE, AUTH, MODEL, OUTPUT)), reason="explicit real-model comparison opt-in required"
)

SOURCE = "def normalize_label(value):\n    return value.strip().lower().replace(' ', '-')\n"
CHECK = (
    "python",
    "-I",
    "-c",
    "exec(open('labels.py').read()); "
    "assert normalize_label('  Hello   WORLD  ')== 'hello-world'; "
    "assert normalize_label('A\\tB\\nC') == 'a-b-c'",
)
REGRESSION = (
    "python",
    "-I",
    "-c",
    "exec(open('labels.py').read()); "
    "assert normalize_label('Hello World')=='hello-world'; "
    "assert normalize_label('')==''; assert normalize_label('   ')==''; "
    "assert normalize_label('already-hyphenated')=='already-hyphenated'",
)
GOAL = (
    "Fix normalize_label in labels.py: collapse every run of whitespace (spaces, tabs and "
    "newlines) to one hyphen, strip surrounding whitespace, and lowercase. Preserve the "
    "empty-string and already-hyphenated behavior. First run the acceptance check to observe "
    "the real existing failure, then correct labels.py. Do not modify .fleet or checks. "
    "Acceptance command: " + json.dumps(CHECK) + ". Regression command: " + json.dumps(REGRESSION)
)


class Stop:
    def cancelled(self):
        return False


def write_report(path, rows):
    path.write_text(canonical_json({"rows": rows}) + "\n")


def test_report_serializes_runtime_frozen_activity(tmp_path):
    path = tmp_path / "comparison.json"
    activity = freeze_json({"exit_code": 1, "type": "item.completed"})
    write_report(path, [{"activity": [activity], "usage": None}])
    assert json.loads(path.read_text())["rows"][0] == {
        "activity": [{"exit_code": 1, "type": "item.completed"}],
        "usage": None,
    }


@live_test
def test_paired_real_model_comparison(tmp_path, monkeypatch, capsys):
    output = Path(OUTPUT)
    output.mkdir(parents=True, exist_ok=False)
    profile = IsolatedWorkerProfile(image=IMAGE, auth_file=AUTH)
    offline = profile.model_copy(update={"auth_file": None})
    stopped = False
    transport_usage = []
    transport_activity = []
    cli_version = None

    class ComparisonExecutor(LocalProcessExecutor):
        def execute(self, request, decision, cancellation):
            nonlocal stopped, cli_version
            if request.argv[0] in {"python", "python3"}:
                self.profile = offline
                return DockerProcessExecutor.execute(self, request, decision, cancellation)
            started = time.monotonic()
            rejection = self._validate_policy(request, decision)
            if rejection:
                return self._result(request, started, failure=rejection)
            assert request.argv[0] == "codex", "no unexpected host executable is permitted"
            if stopped:
                raise RuntimeError("USAGE_LIMIT: entire comparison stopped")
            args = list(request.argv[1:])
            is_model = "exec" in args
            chosen = profile if is_model else offline
            with DockerCandidate(
                chosen,
                self.roots[0],
                seconds=request.timeout_seconds,
                cancellation=cancellation,
                include_untracked=True,
            ) as candidate:
                if is_model:
                    schema_index = args.index("--output-schema") + 1
                    schema = Path(args[schema_index]).read_bytes()
                    code, _, _ = candidate.run(
                        (
                            "python",
                            "-I",
                            "-c",
                            "import sys; "
                            "open('/tmp/proposal-schema.json','wb').write(sys.stdin.buffer.read())",
                        ),
                        stdin=schema,
                    )
                    assert code == 0
                    args[schema_index] = "/tmp/proposal-schema.json"
                    sandbox = args.index("--sandbox")
                    del args[sandbox : sandbox + 2]
                    permission = tuple(
                        p.replace('extends=":workspace"', 'extends=":read-only"')
                        for p in codex_isolated_permission_args()[2:]
                    )
                    args = [
                        "-c",
                        'default_permissions="fleet-isolated"',
                        *permission,
                        *args,
                        "--json",
                        "--output-last-message",
                        "/tmp/final.json",
                        "-c",
                        "features.multi_agent=false",
                        "-c",
                        'web_search="disabled"',
                    ]
                    assert self.stdin_resolver and request.stdin_artifact_digest
                    with self.stdin_resolver(request.stdin_artifact_digest) as stream:
                        prompt = stream.read()

                    def observe(event):
                        nonlocal stopped
                        kind, item = event.get("type"), event.get("item")
                        if isinstance(item, dict) and item.get("type") == "command_execution":
                            transport_activity.append(
                                {
                                    "type": kind,
                                    "id": item.get("id"),
                                    "exit_code": item.get("exit_code"),
                                }
                            )
                        if kind == "turn.completed" and isinstance(event.get("usage"), dict):
                            transport_usage.append(
                                {
                                    k: v
                                    for k, v in event["usage"].items()
                                    if k in {"input_tokens", "cached_input_tokens", "output_tokens"}
                                    and type(v) is int
                                    and v >= 0
                                }
                            )
                        if kind in {"error", "turn.failed"}:
                            detail = json.dumps(event).lower()
                            if any(
                                m in detail
                                for m in (
                                    "usage_limit",
                                    "usage limit",
                                    "rate_limit",
                                    "rate limit",
                                    "insufficient_quota",
                                )
                            ):
                                stopped = True
                                raise RuntimeError("USAGE_LIMIT: entire comparison stopped")

                    code, _, native_stderr = candidate.run(
                        ("codex", *args), stdin=prompt, observe=observe
                    )
                    if code and _reports_usage_limit(native_stderr.decode(errors="replace")):
                        stopped = True
                        raise RuntimeError("USAGE_LIMIT: entire comparison stopped")
                    if code == 0:
                        code, stdout, _ = candidate.run(
                            (
                                "python",
                                "-I",
                                "-c",
                                "import sys; "
                                "sys.stdout.buffer.write(open('/tmp/final.json','rb').read())",
                            )
                        )
                    else:
                        stdout = b""
                else:
                    code, stdout, _ = candidate.run(("codex", *args))
                    if args == ["--version"]:
                        cli_version = stdout.decode().strip()
            eid = identifier("comparison-execution")
            return ExecutionResult(
                id=eid,
                run_id=request.run_id,
                created_at=now(),
                request_digest=request.content_digest,
                exit_code=code,
                status="succeeded" if code == 0 else "failed",
                failure=None
                if code == 0
                else StableFailure(
                    code=StableFailureCode.PROCESS_FAILED,
                    message=f"comparison worker exited with code {code}",
                ),
                duration_seconds=time.monotonic() - started,
                stdout_artifact_digest=self._store_output(request, eid, stdout, "process_stdout"),
                stderr_artifact_digest=self._store_output(request, eid, b"", "process_stderr"),
            )

    monkeypatch.setattr(cli, "LocalProcessExecutor", ComparisonExecutor)
    rows = []
    for arm in ("proposal", "isolated"):
        assert not stopped, "usage limit stops both arms, without fallback or reset"
        case = tmp_path / arm
        root = case / "repo"
        (root / ".fleet").mkdir(parents=True)
        (root / "labels.py").write_text(SOURCE)
        harness = {
            "schema_version": 2,
            "commands": {"acceptance": {"argv": CHECK}, "regression": {"argv": REGRESSION}},
            "paths": {"writable": ["labels.py"], "protected": [".git/**", ".fleet/**"]},
            "verification": {"required": ["acceptance", "regression"]},
            "worker": {
                "allowed": ["codex_cli"],
                "allowed_strategy_ids": ["comparison"],
                "isolated_workspace_tools": True,
            },
            "budgets": {"wall_seconds": 240.0, "processes": 40, "worker_turns": 1},
        }
        (root / ".fleet/project.json").write_text(json.dumps(harness))
        for args in (
            ("init", "-q"),
            ("add", "."),
            (
                "-c",
                "user.name=Fleet Evaluation",
                "-c",
                "user.email=fleet@example.invalid",
                "commit",
                "-qm",
                "comparison base",
            ),
        ):
            subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
        tree = (
            subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD^{tree}"])
            .decode()
            .strip()
        )
        with DockerCandidate(offline, root, seconds=40, cancellation=Stop()) as candidate:
            before, _, _ = candidate.run(CHECK)
            regression_before, _, _ = candidate.run(REGRESSION)
        assert before != 0 and regression_before == 0
        config = {
            "schema_version": 1,
            "workers": {"codex_cli": {"executable": "codex"}},
            "routing": {
                "default_strategy_set": "comparison",
                "default_assessment_strategy": "comparison",
                "strategy_sets": {"comparison": ["comparison"]},
                "strategies": [
                    {
                        "id": "comparison",
                        "backend": "codex_cli",
                        "model": MODEL,
                        "effort": "low",
                        "capabilities": ["edit_intent", "process"],
                        "planner_eligible": True,
                    }
                ],
            },
        }
        if arm == "isolated":
            config["isolated_worker"] = json.loads(profile.model_dump_json())
        operator = case / "operator.json"
        operator.write_text(json.dumps(config))
        operator.chmod(0o600)
        database = case / "fleet.db"
        monkeypatch.setattr(cli, "resolve_database_path", lambda *a, db=database, **kw: db)
        monkeypatch.delenv("FLEET_DB", raising=False)
        started = time.monotonic()
        code = cli.main(
            [
                "work",
                GOAL,
                "--repo",
                str(root),
                "--operator-config",
                str(operator),
                "--routing-mode",
                "fixed",
                "--strategy",
                "comparison",
                "--non-interactive",
            ]
        )
        wall = time.monotonic() - started
        emitted = capsys.readouterr().out
        result = json.loads(emitted)
        with SQLiteStore(database) as store:
            workers = store.list_records("worker_result_v2", WorkerResult)
            parents = store.list_records(
                "parent_candidate_evaluation_v2", ParentCandidateEvaluationRecord
            )
            verifications = store.list_records("verification_result_v2", ExecutionResult)
        native = [w for w in workers if w.resource_usage and w.resource_usage.get("isolation")]
        activity = (
            [a for w in native for a in w.resource_usage.get("local_activity", [])]
            if arm == "isolated"
            else list(transport_activity)
        )
        usage = [dict(w.usage) for w in native if w.usage] if arm == "isolated" else transport_usage
        failures = [w.failure.code.value for w in workers if w.failure]
        stopped = stopped or any(w.failure and "USAGE_LIMIT" in w.failure.message for w in workers)
        row = {
            "arm": arm,
            "model": MODEL,
            "effort": "low",
            "runtime_image": IMAGE,
            "cli_version": cli_version,
            "source_tree": tree,
            "exit_code": code,
            "status": result["status"],
            "wall_seconds": wall,
            "worker_results": len(workers),
            "failures": failures,
            "activity": activity,
            "usage": usage or None,
            "human_active_seconds": None,
            "human_interventions": 0,
            "cost_usd": None,
            "source_unchanged": (root / "labels.py").read_text() == SOURCE,
            "proposal_digests": [p.content_digest for w in workers for p in w.proposals],
            "candidate_artifact_digests": [p.candidate_artifact_digest for p in parents],
            "independent_verification_statuses": [v.status for v in verifications],
            "worker_adapter_seconds": [w.duration_seconds for w in workers],
            "usage_limit": stopped,
        }
        rows.append(row)
        write_report(output / "comparison.json", rows)
        assert row["source_unchanged"]
        if stopped:
            pytest.fail("USAGE_LIMIT: comparison stopped; partial report preserved")
    assert rows[0]["source_tree"] == rows[1]["source_tree"]
    # Failed tasks remain data, not a reason to retry until a favorable result appears.
