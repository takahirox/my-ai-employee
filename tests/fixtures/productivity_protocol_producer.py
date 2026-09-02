#!/usr/bin/env python3
"""Deterministic offline producer exercising the real protocol subprocess boundary."""

from __future__ import annotations

import argparse
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from ai_employee.productivity_evaluation import (
    ArmIdentity,
    CheckDisposition,
    CheckOutcome,
    ResultBundle,
    TaskIdentity,
    TerminalOutcome,
    TrialMetrics,
    TrialResult,
    dump_result_bundle,
    trial_id,
)
from ai_employee.productivity_protocol import (
    ProtocolCheckArtifact,
    ProtocolCheckRecord,
    load_protocol_manifest,
)
from ai_employee.serialization import canonical_digest, canonical_json, loads_model


def _dump(value: object) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _arm(manifest_path: Path, arm_id: str, mode: str) -> ArmIdentity:
    manifest, _ = load_protocol_manifest(manifest_path)
    protocol = next(item for item in manifest.protocols if item.id == "codex-direct")
    treatment = protocol.treatments[0]
    environment = treatment.environment
    fairness = treatment.fairness_config
    if mode == "environment-version":
        environment = environment.model_copy(update={"executable_version": "fictional-2"})
    elif mode == "dependency-lock":
        environment = environment.model_copy(update={"dependency_lock_digest": "f" * 64})
    elif mode == "sandbox-network":
        environment = environment.model_copy(update={"sandbox_mode": "fictional"})
    elif mode == "cache-machine":
        environment = environment.model_copy(update={"cache_policy": "warm"})
    elif mode == "prompt-context":
        fairness = fairness.model_copy(update={"context_digest": "e" * 64})
    elif mode == "model":
        fairness = fairness.model_copy(update={"reasoning_effort": "fictional"})
    elif mode == "tools":
        fairness = fairness.model_copy(update={"tools": ("browser", *fairness.tools)})
    elif mode == "budgets":
        fairness = fairness.model_copy(
            update={"budgets": fairness.budgets.model_copy(update={"wall_seconds": 3601.0})}
        )
    elif mode == "stopping":
        fairness = fairness.model_copy(
            update={"stopping": fairness.stopping.model_copy(update={"maximum_retries": 99})}
        )
    elif mode == "pricing":
        fairness = fairness.model_copy(
            update={
                "pricing": fairness.pricing.model_copy(
                    update={"subscription_allocation": "fictional"}
                )
            }
        )
    elif mode == "randomized-order":
        fairness = fairness.model_copy(
            update={"randomized_order": tuple(reversed(fairness.randomized_order))}
        )
    if arm_id != treatment.id:
        fairness = fairness.model_copy(
            update={"randomized_order": (arm_id, *fairness.randomized_order[1:])}
        )
    return ArmIdentity(
        id=arm_id,
        kind=treatment.kind,
        adapter=treatment.adapter,
        worker=treatment.worker,
        environment=environment,
        fairness_config=fairness,
        arm_config=treatment.arm_config,
        environment_digest=canonical_digest(environment),
        fairness_config_digest=canonical_digest(fairness),
        arm_config_digest=canonical_digest(treatment.arm_config),
        seed=1,
        repetition=1,
        assignment_index=fairness.randomized_order.index(arm_id),
        disabled_components=treatment.disabled_components,
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
    parser.add_argument("--manifest", required=True)
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
            "environment-version",
            "dependency-lock",
            "sandbox-network",
            "cache-machine",
            "prompt-context",
            "model",
            "tools",
            "budgets",
            "stopping",
            "pricing",
            "randomized-order",
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
    arm = _arm(
        Path(args.manifest),
        "unexpected-arm" if args.mode == "wrong-arm" else "codex-direct",
        args.mode,
    )
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
