"""Explicit private History selection, restore and single-trial invocation."""

from __future__ import annotations

import argparse
from pathlib import Path

from pydantic import ValidationError

from .history_corpus import (
    CorpusEnvironment,
    inspect_corpus,
    load_task,
    restore_task,
    write_private,
)
from .history_reporting import comparison_report
from .inspector import _open_read_only_store
from .serialization import canonical_json


def add_corpus_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("corpus", help="private History-derived task fixtures")
    actions = parser.add_subparsers(dest="corpus_action", required=True)
    inspect = actions.add_parser(
        "inspect", help="read-only reproducibility reasons; no Goal bodies"
    )
    export = actions.add_parser("export", help="explicitly export one private task input")
    for action in (inspect, export):
        action.add_argument("--history-db", required=True)
        action.add_argument("--environment-file", required=action is export)
        action.add_argument("--run", action="append", required=action is export)
        action.add_argument("--task-class", default="unclassified")
    export.add_argument("--output", required=True)
    restore = actions.add_parser("restore", help="restore a private clean clone; never run a model")
    execute = actions.add_parser("run", help="explicitly execute one fresh corpus trial")
    for action in (restore, execute):
        action.add_argument("--fixture", required=True)
        action.add_argument("--directory", required=True)
    execute.add_argument("--operator-config", required=True)
    execute.add_argument("--profile", choices=("lightweight", "adaptive"), required=True)
    execute.add_argument("--strategy")
    execute.add_argument("--minimal-sufficient", choices=("on", "off"), default="on")
    execute.add_argument("--run-id", required=True)
    execute.add_argument(
        "--execute", action="store_true", help="explicit consent to configured model execution"
    )
    report = actions.add_parser("report", help="read-only quality-first trial comparison")
    report.add_argument("--fixture", required=True)
    report.add_argument("--history-db", required=True)
    report.add_argument("--run", action="append", required=True)


def run_corpus(args: argparse.Namespace) -> int:
    if args.corpus_action in {"inspect", "export"}:
        environment = None
        if args.environment_file is not None:
            path = Path(args.environment_file)
            if path.stat().st_size > 2_000_000:
                raise ValueError("environment declaration exceeds the supported input bound")
            try:
                environment = CorpusEnvironment.model_validate_json(path.read_bytes())
            except ValueError:
                raise ValueError("environment declaration failed strict validation") from None
        with _open_read_only_store(args.history_db) as store:
            candidates = inspect_corpus(
                store, environment, run_ids=tuple(args.run or ()), task_class=args.task_class
            )
        if args.corpus_action == "export":
            if len(candidates) != 1 or candidates[0].task is None:
                raise ValueError(
                    "export requires exactly one REPRODUCIBLE selection; inspect its reasons first"
                )
            write_private(Path(args.output), candidates[0].task)
        print(
            canonical_json(
                [
                    item.model_dump(mode="json", exclude={"task"})
                    | {
                        "logical_task_digest": None
                        if item.task is None
                        else item.task.logical_task_digest,
                        "historical_status": None
                        if item.task is None
                        else item.task.historical_status,
                        "historical_replans": None
                        if item.task is None
                        else item.task.historical_replans,
                        "task_class": args.task_class,
                    }
                    for item in candidates
                ]
            )
        )
        return 0
    task = load_task(Path(args.fixture))
    if args.corpus_action == "report":
        with _open_read_only_store(args.history_db) as store:
            try:
                report = comparison_report(store, task, tuple(args.run))
            except ValidationError:
                raise ValueError("stored trial evidence failed strict validation") from None
        print(canonical_json(report))
        return 0
    if args.corpus_action == "run" and not args.execute:
        raise ValueError("--execute is required; inspect/restore never authorize model calls")
    destination = restore_task(task, Path(args.directory).absolute())
    if args.corpus_action == "restore":
        print(
            canonical_json(
                {
                    "restored": True,
                    "base_commit": task.start.base_commit,
                    "planning": "fresh_from_original_goal",
                }
            )
        )
        return 0
    from . import cli

    argv = [
        "work",
        task.start.goal.statement,
        "--repo",
        str(destination),
        "--operator-config",
        args.operator_config,
        "--profile",
        args.profile,
        "--task-fixture",
        str(Path(args.fixture).resolve()),
        "--run-id",
        args.run_id,
        "--task-kind",
        task.start.goal.task_kind.value,
        "--non-interactive",
    ]
    if args.strategy is not None:
        argv.extend(("--strategy", args.strategy))
    argv.extend(("--minimal-sufficient", args.minimal_sufficient))
    argv.append(
        "--allow-processes" if task.start.goal.processes_authorized else "--no-allow-processes"
    )
    return cli.main(argv)
