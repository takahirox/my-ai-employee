#!/usr/bin/env python3
"""Deterministic offline producer exercising the real protocol subprocess boundary."""

from __future__ import annotations

import argparse
import os
import sys
import time
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
from ai_employee.productivity_protocol import load_protocol_manifest
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


def _evaluate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--trial", required=True)
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--check", required=True)
    parser.add_argument("--mode", choices=("passed", "failed", "oversized"), required=True)
    args = parser.parse_args(argv)
    if Path(args.repository).resolve() != Path.cwd().resolve() or not Path(args.task).is_file():
        return 9
    if not args.trial or args.protocol != "codex-direct":
        return 8
    if args.mode == "oversized":
        sys.stdout.buffer.write(b"x" * 1_000_001)
    else:
        print(f"{args.trial}:{args.check}:{args.mode}")
    return 1 if args.mode == "failed" else 0


def main() -> int:
    if sys.argv[1:2] == ["evaluator"]:
        return _evaluate(sys.argv[2:])
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
            "claim-artifact",
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
            "replace-stage",
            "symlink-output",
            "lingering-child",
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
    result = TrialResult(
        id=bound_trial_id,
        task=task,
        arm=arm,
        acceptance_outcomes=tuple(
            CheckOutcome(
                check_id=item.id,
                authority=item.authority,
                disposition=CheckDisposition.PASSED,
                evidence_digest="0" * 64,
            )
            for item in task.acceptance_criteria
        ),
        regression_outcomes=tuple(
            CheckOutcome(
                check_id=item.id,
                authority=item.authority,
                disposition=CheckDisposition.PASSED,
                evidence_digest="0" * 64,
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
    if args.mode == "replace-stage":
        moved = output.with_name(output.name + "-moved")
        output.rename(moved)
        output.symlink_to(moved, target_is_directory=True)
        output = moved
    if args.mode == "symlink-output":
        external = output.with_name(output.name + "-draft.json")
        external.write_bytes(bundle_bytes)
        (output / "result-bundle.json").symlink_to(external)
        return 0
    if args.mode != "missing":
        (output / "result-bundle.json").write_bytes(bundle_bytes)
    if args.mode == "lingering-child":
        directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
        child = os.fork()
        if child == 0:
            os.setsid()
            with open(os.devnull, "rb") as source, open(os.devnull, "ab") as sink:
                os.dup2(source.fileno(), 0)
                os.dup2(sink.fileno(), 1)
                os.dup2(sink.fileno(), 2)
            time.sleep(0.5)
            os.chmod("result-bundle.json", 0o600, dir_fd=directory)
            descriptor = os.open("result-bundle.json", os.O_WRONLY | os.O_TRUNC, dir_fd=directory)
            os.write(descriptor, b"late mutation\n")
            os._exit(0)
        os.close(directory)
        time.sleep(0.1)
    if args.mode == "claim-artifact":
        (output / "acceptance.json").write_bytes(b"{}\n")
    if args.mode == "extra":
        (output / "extra.txt").write_text("unexpected", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
