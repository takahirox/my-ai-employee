"""Operator commands for the autonomous runtime, with explicit snapshotted configuration."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .candidates import Candidates
from .capabilities import SUPPORTED_BACKENDS
from .container import ContainerModel
from .engine import Engine, Waiting
from .history import Journal, Stopped
from .isolated_worker import IsolatedWorkerProfile
from .models import RunConfig, StagePolicy

PUBLIC_CONTRACT = "fleet-run-1"


def emit(body: dict[str, object]) -> None:
    print(json.dumps({"contract_version": PUBLIC_CONTRACT, **body}, ensure_ascii=False), flush=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="fleet", description="Goal-driven autonomous worker orchestration"
    )
    result.add_argument("--state", type=Path, default=Path.home() / ".fleet" / "autonomous")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("history", help="list recent runs")
    inspector = commands.add_parser("serve", help="open the read-only local Inspector")
    inspector.add_argument("--port", type=int, default=8765)
    initialize = commands.add_parser("init", help="write an operator configuration template")
    initialize.add_argument("--model", required=True)
    initialize.add_argument("--backend", choices=SUPPORTED_BACKENDS, default="codex")
    initialize.add_argument("--image", required=True, help="immutable prepared Docker image ID")
    initialize.add_argument(
        "--auth-file", type=Path, required=True, help="explicit delegated model authentication"
    )
    initialize.add_argument("--output", type=Path, default=Path("fleet-run.json"))
    for name in ("submit", "work"):
        work = commands.add_parser(
            name, help="create a Run" if name == "submit" else "create and execute a Run"
        )
        work.add_argument("goal")
        work.add_argument("--config", type=Path, required=True)
        work.add_argument("--root", type=Path, default=Path.cwd())
    for name in ("inspect", "status", "logs", "result", "resume", "cancel", "cleanup", "revoke"):
        commands.add_parser(name).add_argument("run_id")
    revise = commands.add_parser("revise", help="explicitly replace the Goal in a linked new Run")
    revise.add_argument("run_id")
    revise.add_argument("goal")
    answer = commands.add_parser("answer", help="answer pending clarification")
    answer.add_argument("run_id")
    answer.add_argument("answer")
    approve = commands.add_parser("authority", help="approve or reject a pending authority request")
    approve.add_argument("run_id")
    approve.add_argument("attempt_id")
    approve.add_argument("--decision", choices=("approve", "reject"), required=True)
    promote = commands.add_parser("promote", help="publish a verified result to a new directory")
    promote.add_argument("run_id")
    promote.add_argument("--destination", type=Path, required=True)
    return result


def projection(journal: Journal, run: str) -> dict[str, object]:
    events = journal.events(run)
    if not events:
        raise KeyError(run)
    kinds = [event["kind"] for event in events]
    config = journal.config(run)
    resources = {
        event["body"]["task_digest"]: event["kind"]
        for event in events
        if event["kind"] in {"resource_wait", "resource_acquired", "resource_released"}
    }
    clarification = [
        kind for kind in kinds if kind in {"clarification_wait", "clarification_answer", "goal"}
    ]
    status = "running"
    terminal_before_cleanup = False
    stopped = False
    for event in events:
        if event["kind"] in {"failed", "uncertain"}:
            terminal_before_cleanup = True
        elif event["kind"] == "stopped":
            stopped |= event["body"]["reason"] != "CLEANUP_REQUESTED" or not terminal_before_cleanup
    if stopped:
        status = "stopped"
    elif "completed" in kinds:
        status = "completed"
    elif "uncertain" in kinds:
        status = "uncertain"
    elif {event["body"]["attempt"] for event in events if event["kind"] == "approval_wait"} - {
        event["body"]["attempt"]
        for event in events
        if event["kind"] in {"authority_applied", "authority_rejected"}
    }:
        status = "paused_for_approval"
    elif clarification and clarification[-1] == "clarification_wait":
        status = "waiting_for_clarification"
    elif clarification and clarification[-1] == "clarification_answer":
        status = "ready_to_resume"
    elif "resource_wait" in resources.values():
        status = "waiting_for_resource"
    elif "failed" in kinds:
        status = "failed"
    cleanup = "not_requested"
    for event in events:
        if event["kind"] == "reserved":
            cleanup = "not_requested"
        elif event["kind"] == "cleanup_requested":
            cleanup = "pending"
        elif event["kind"] == "cleanup_confirmed":
            cleanup = "confirmed"
        elif event["kind"] == "cleanup_failed":
            cleanup = "unconfirmed"
    return {
        "contract_version": PUBLIC_CONTRACT,
        "run_id": run,
        "status": status,
        "cleanup": cleanup,
        "external_outcome_uncertain": "uncertain" in kinds,
        "budget": journal.budget(run),
        "stage_diagnostics": [
            event["body"]
            for event in events
            if event["kind"] in {"output_rejected", "review_diagnostic", "preflight", "readiness"}
        ],
        "stage_invocations": [
            {key: event["body"].get(key) for key in ("stage", "call_key", "ordinal", "binding")}
            for event in events
            if event["kind"] == "reserved"
        ],
        "policy": config.model_dump(mode="json", exclude={"isolation", "checks"}),
        "goal": next(
            (event["body"]["goal"] for event in reversed(events) if event["kind"] == "goal"), None
        ),
        "plan": next(
            (event["body"]["plan"] for event in reversed(events) if event["kind"] == "plan"), None
        ),
        "events": events,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return _command(args)
    except (Stopped, Waiting, ValueError, OSError, KeyError, RuntimeError) as error:
        emit({"error": str(error)[:300]})
        return 2


def _command(args: argparse.Namespace) -> int:
    if args.command == "init":
        stage = StagePolicy(backend=args.backend, model=args.model)
        config = RunConfig(
            clarification=stage.model_copy(update={"review": "always"}),
            planning=stage,
            worker=stage,
            verification=stage,
            recovery=stage,
            isolation=IsolatedWorkerProfile(
                image=args.image, auth_file=str(args.auth_file.resolve())
            ),
        )
        with args.output.open("x") as stream:
            stream.write(config.model_dump_json(indent=2) + "\n")
        emit({"configuration": str(args.output)})
        return 0
    root = args.state.resolve()
    journal = Journal(root / "history.db")
    if args.command == "serve":
        from .inspector import serve

        serve(journal, args.port)
        return 0
    if args.command == "history":
        from .inspector import run_list

        emit({"runs": run_list(journal)})
        return 0
    run: str | None = getattr(args, "run_id", None)
    try:
        config = (
            RunConfig.model_validate_json(args.config.read_text())
            if args.command in {"submit", "work"}
            else journal.config(str(run))
        )
        engine = Engine(
            journal,
            Candidates(root / "objects"),
            ContainerModel(config.isolation),
            root / "workspaces",
        )
        if args.command in {"submit", "work"}:
            run = engine.prepare(args.goal, config, args.root.resolve())
            # The ID is available even if native preflight/model startup fails.
            emit({"run_id": run})
            if args.command == "submit":
                return 0
            engine.execute(run)
        else:
            assert run is not None
            if args.command == "resume":
                engine.execute(run)
            elif args.command == "revise":
                run = engine.revise_goal(run, args.goal)
                emit({"run_id": run})
                engine.execute(run)
            elif args.command == "cancel":
                journal.stop(run, "OPERATOR_CANCELLED")
            elif args.command == "cleanup":
                engine.cleanup(run)
            elif args.command == "result":
                candidate = engine.result(run)
                emit(
                    {
                        "run_id": run,
                        "candidate": candidate.model_dump(mode="json"),
                        "candidate_digest": candidate.digest,
                    }
                )
                return 0
            elif args.command == "revoke":
                engine.revoke_authority(run)
            elif args.command == "answer":
                engine.answer(run, args.answer)
            elif args.command == "authority":
                engine.approve_authority(run, args.attempt_id, approve=args.decision == "approve")
            elif args.command == "promote":
                engine.promote(run, args.destination)
        assert run is not None
        view = projection(journal, run)
        emit(view)
        return (
            0
            if view["status"] == "completed"
            or args.command
            in {
                "inspect",
                "status",
                "logs",
                "answer",
                "authority",
                "cancel",
                "cleanup",
                "revoke",
                "promote",
            }
            else 2
        )
    except (Stopped, Waiting, ValueError, OSError, KeyError, RuntimeError) as error:
        emit({"run_id": run, "error": str(error)[:300]})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
