#!/usr/bin/env python3
"""Deterministic offline producer exercising the real protocol subprocess boundary."""

from __future__ import annotations

import argparse
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from ai_employee.productivity_evaluation import (
    ArmConfigManifest,
    ArmIdentity,
    ArmKind,
    CheckDisposition,
    CheckOutcome,
    EnvironmentManifest,
    FairnessConfigManifest,
    PricingManifest,
    ResourceBudgetManifest,
    ResultBundle,
    StoppingManifest,
    TaskIdentity,
    TerminalOutcome,
    TrialMetrics,
    TrialResult,
    dump_result_bundle,
    trial_id,
)
from ai_employee.productivity_protocol import ProtocolCheckArtifact, ProtocolCheckRecord
from ai_employee.serialization import canonical_digest, canonical_json, loads_model


def _dump(value: object) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _arm(arm_id: str) -> ArmIdentity:
    environment = EnvironmentManifest(
        executable=str(Path(__file__).resolve()),
        executable_version="fixture-1",
        dependency_lock_digest="1" * 64,
        sandbox_mode="workspace-write",
        network_mode="disabled",
        cache_policy="cold",
        machine_digest="2" * 64,
    )
    fairness = FairnessConfigManifest(
        prompt_digest="3" * 64,
        context_digest="4" * 64,
        model_provider="fixture",
        model_name="fixture",
        model_version="fixture-1",
        reasoning_effort="fixed",
        tools=("edit_intent", "process"),
        budgets=ResourceBudgetManifest(
            wall_seconds=60,
            input_tokens=1000,
            output_tokens=1000,
        ),
        stopping=StoppingManifest(
            maximum_attempts=1,
            maximum_retries=0,
            maximum_repairs=0,
            terminal_conditions=("accepted",),
        ),
        pricing=PricingManifest(
            currency="USD",
            input_per_million=0,
            output_per_million=0,
            subscription_allocation="fixture",
        ),
        randomized_order=(arm_id, "unused-control"),
    )
    config = ArmConfigManifest(
        planning=False,
        review=False,
        repair=False,
        maximum_parallelism=1,
    )
    return ArmIdentity(
        id=arm_id,
        kind=ArmKind.DIRECT_AGENT,
        adapter="codex",
        worker="codex",
        environment=environment,
        fairness_config=fairness,
        arm_config=config,
        environment_digest=canonical_digest(environment),
        fairness_config_digest=canonical_digest(fairness),
        arm_config_digest=canonical_digest(config),
        seed=1,
        repetition=1,
        assignment_index=0,
    )


def _artifact(
    family: str,
    bound_trial_id: str,
    task: TaskIdentity,
) -> ProtocolCheckArtifact:
    definitions = task.acceptance_criteria if family == "acceptance" else task.regression_checks
    return ProtocolCheckArtifact(
        format="fleet-productivity-check-artifact/1",
        family=family,
        outcomes=tuple(
            ProtocolCheckRecord(
                trial_id=bound_trial_id,
                check_id=item.id,
                authority=item.authority,
                disposition=CheckDisposition.PASSED,
            )
            for item in definitions
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--protocol", required=True)
    parser.add_argument(
        "--mode",
        default="valid",
        choices=(
            "valid",
            "bad-evidence",
            "extra",
            "missing",
            "noncanonical-bundle",
            "nonzero",
            "wrong-arm",
            "wrong-task",
        ),
    )
    args = parser.parse_args()
    if Path(args.repository).resolve() != Path.cwd().resolve():
        return 9
    if args.protocol != "codex-direct":
        return 8
    if args.mode == "nonzero":
        return 7
    task = loads_model(Path(args.task).read_bytes(), TaskIdentity)
    if args.mode == "wrong-task":
        task = task.model_copy(update={"task_version": "stale-v2"})
    arm = _arm("unexpected-arm" if args.mode == "wrong-arm" else "codex-direct")
    bound_trial_id = trial_id(task, arm)
    acceptance = _artifact("acceptance", bound_trial_id, task)
    regression = _artifact("regression", bound_trial_id, task)
    acceptance_bytes = _dump(acceptance)
    regression_bytes = _dump(regression)
    acceptance_digest = hashlib.sha256(acceptance_bytes).hexdigest()
    regression_digest = hashlib.sha256(regression_bytes).hexdigest()
    if args.mode == "bad-evidence":
        acceptance_digest = "0" * 64
    result = TrialResult(
        id=bound_trial_id,
        task=task,
        arm=arm,
        acceptance_outcomes=tuple(
            CheckOutcome(
                check_id=item.id,
                authority=item.authority,
                disposition=CheckDisposition.PASSED,
                evidence_digest=acceptance_digest,
            )
            for item in task.acceptance_criteria
        ),
        regression_outcomes=tuple(
            CheckOutcome(
                check_id=item.id,
                authority=item.authority,
                disposition=CheckDisposition.PASSED,
                evidence_digest=regression_digest,
            )
            for item in task.regression_checks
        ),
        terminal_outcome=TerminalOutcome.ACCEPTED,
        process_exit_code=0,
        metrics=TrialMetrics(
            human_active_seconds=1,
            human_interventions=0,
            time_to_accepted_seconds=2,
            wall_seconds=2,
            input_tokens=10,
            output_tokens=10,
            api_cost=0,
            compute_seconds=2,
            compute_cost=0,
            retries=0,
            repairs=0,
            replans=0,
            escalations=0,
            recoveries=0,
            decomposed_nodes=1,
            dependency_edges=0,
            maximum_parallelism=1,
            critical_path_seconds=2,
            context_input_tokens=10,
            context_output_tokens=10,
            unnecessary_work_items=0,
            unnecessary_work_seconds=0,
        ),
    )
    bundle = ResultBundle(
        id=args.protocol,
        run_id=args.protocol,
        created_at=datetime(2026, 9, 3, tzinfo=UTC),
        benchmark=task.benchmark,
        benchmark_version=task.benchmark_version,
        results=(result,),
    )
    bundle_bytes = dump_result_bundle(bundle)
    if args.mode == "noncanonical-bundle":
        bundle_bytes = bundle_bytes.replace(b":", b": ", 1)
    output = Path(args.output)
    (output / "acceptance.json").write_bytes(acceptance_bytes)
    if args.mode != "missing":
        (output / "regression.json").write_bytes(regression_bytes)
    (output / "result-bundle.json").write_bytes(bundle_bytes)
    if args.mode == "extra":
        (output / "extra.txt").write_text("unexpected", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
