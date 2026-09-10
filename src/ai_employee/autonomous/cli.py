"""Operator commands for the autonomous runtime, with explicit snapshotted configuration."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .candidates import Candidates
from .engine import Engine
from .history import Journal, Stopped
from .models import RunConfig, StagePolicy
from .native import NativeModel


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="fleet", description="Goal-driven autonomous worker orchestration"
    )
    result.add_argument("--state", type=Path, default=Path.home() / ".fleet" / "autonomous")
    commands = result.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="write an operator configuration template")
    initialize.add_argument("--model", required=True)
    initialize.add_argument("--backend", choices=("codex", "claude"), default="codex")
    initialize.add_argument("--output", type=Path, default=Path("fleet-run.json"))
    work = commands.add_parser("work", help="clarify, plan, execute and verify a goal")
    work.add_argument("goal")
    work.add_argument("--config", type=Path, required=True)
    work.add_argument("--root", type=Path, default=Path.cwd())
    for name in ("inspect", "logs", "resume", "cancel"):
        commands.add_parser(name).add_argument("run_id")
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
    kinds = [event["kind"] for event in events]
    status = "running"
    if "stopped" in kinds:
        status = "stopped"
    elif "completed" in kinds:
        status = "completed"
    elif "uncertain" in kinds:
        status = "uncertain"
    elif "approval_wait" in kinds and kinds.index("approval_wait") > max(
        (index for index, kind in enumerate(kinds) if kind == "authority_applied"), default=-1
    ):
        status = "paused_for_approval"
    elif "clarification_wait" in kinds and "goal" not in kinds:
        status = "waiting_for_clarification"
    elif "failed" in kinds:
        status = "failed"
    return {
        "run_id": run,
        "status": status,
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
    if args.command == "init":
        stage = StagePolicy(backend=args.backend, model=args.model)
        config = RunConfig(
            clarification=stage.model_copy(update={"review": "always"}),
            planning=stage,
            worker=stage,
            verification=stage,
            recovery=stage,
        )
        with args.output.open("x") as stream:
            stream.write(config.model_dump_json(indent=2) + "\n")
        print(json.dumps({"configuration": str(args.output)}))
        return 0
    root = args.state.resolve()
    journal = Journal(root / "history.db")
    engine = Engine(journal, Candidates(root / "objects"), NativeModel(), root / "workspaces")
    run: str | None = getattr(args, "run_id", None)
    try:
        if args.command == "work":
            config = RunConfig.model_validate_json(args.config.read_text())
            run = engine.prepare(args.goal, config, args.root.resolve())
            # The ID is available even if native preflight/model startup fails.
            print(json.dumps({"run_id": run}), flush=True)
            engine.execute(run)
        else:
            assert run is not None
            if args.command == "resume":
                engine.execute(run)
            elif args.command == "cancel":
                journal.stop(run, "OPERATOR_CANCELLED")
            elif args.command == "answer":
                engine.answer(run, args.answer)
            elif args.command == "authority":
                engine.approve_authority(run, args.attempt_id, approve=args.decision == "approve")
            elif args.command == "promote":
                engine.promote(run, args.destination)
        assert run is not None
        view = projection(journal, run)
        print(json.dumps(view, ensure_ascii=False))
        return (
            0
            if view["status"] == "completed"
            or args.command in {"inspect", "logs", "answer", "authority", "cancel"}
            else 2
        )
    except (Stopped, ValueError, OSError, KeyError) as error:
        print(json.dumps({"run_id": run, "error": str(error)[:300]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
