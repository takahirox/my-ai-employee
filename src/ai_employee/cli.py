"""Command-line interface for the local deterministic Fleet runtime."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .demo import run_demo
from .domain import (
    ContractKind,
    ExecutionPolicy,
    Goal,
    Graph,
    ResultEnvelope,
    ResultStatus,
    Run,
)
from .domain.base import freeze_json
from .graph import accept_graph
from .inspector import compare_runs, inspect_run, serve
from .project import discover_project, migration_candidate, write_migration_candidate
from .runtime import DeterministicRuntime, NodeExecutionContext
from .serialization import canonical_json, loads_yaml_model
from .storage import SQLiteStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet", description="My AI Employee fleet runtime")
    parser.add_argument("--version", action="version", version=f"fleet {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    demo = commands.add_parser("demo", help="run the deterministic offline demonstration")
    demo.add_argument("--db", default=".fleet/fleet.db")
    demo.add_argument("--run-id", default=None)

    run = commands.add_parser("run", help="run a declarative YAML/JSON graph")
    run.add_argument("graph")
    run.add_argument("--goal", default="Execute the accepted declarative graph")
    run.add_argument("--run-id", default=None)
    run.add_argument("--db", default=".fleet/fleet.db")
    run.add_argument("--pause-after", type=int)

    inspect = commands.add_parser("inspect", help="inspect a persisted run")
    inspect.add_argument("run_id")
    inspect.add_argument("--db", default=".fleet/fleet.db")

    replay = commands.add_parser("replay", help="replay stored control flow without workers")
    replay.add_argument("run_id")
    replay.add_argument("--db", default=".fleet/fleet.db")

    resume = commands.add_parser("resume", help="resume a paused run")
    resume.add_argument("run_id")
    resume.add_argument("--db", default=".fleet/fleet.db")

    for name in ("pause", "cancel"):
        control = commands.add_parser(name, help=f"request {name} at the next node boundary")
        control.add_argument("run_id")
        control.add_argument("--db", default=".fleet/fleet.db")

    compare = commands.add_parser("compare", help="compare two stored runs and strategies")
    compare.add_argument("left_run_id")
    compare.add_argument("right_run_id")
    compare.add_argument("--db", default=".fleet/fleet.db")

    server = commands.add_parser("serve", help="serve the read-only local Inspector")
    server.add_argument("--db", default=".fleet/fleet.db")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)

    project = commands.add_parser("project", help="show explicit or provisional ProjectProfile")
    project.add_argument("root", nargs="?", default=".")
    project.add_argument("--migrate", action="store_true", help="render a safe v2 candidate")
    project.add_argument("--output", help="write the migration candidate to this new path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "project":
        if args.output and not args.migrate:
            build_parser().error("--output requires --migrate")
        if args.migrate:
            if args.output:
                destination = write_migration_candidate(args.root, args.output)
                print(canonical_json({"output": str(destination)}))
            else:
                print(migration_candidate(args.root), end="")
        else:
            print(canonical_json(discover_project(args.root)))
        return 0
    with SQLiteStore(args.db) as store:
        if args.command == "demo":
            run_id = args.run_id or f"demo-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
            outcome = run_demo(store, run_id=run_id)
            print(
                canonical_json(
                    {
                        "run_id": run_id,
                        "state": outcome.run.state.value,
                        "coverage": outcome.coverage,
                    }
                )
            )
        elif args.command == "run":
            graph = loads_yaml_model(Path(args.graph).read_text(encoding="utf-8"), Graph)
            run_id = args.run_id or f"run-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
            policy = ExecutionPolicy(
                max_nodes=graph.budget.max_nodes,
                max_attempts=graph.budget.max_attempts,
                max_wall_seconds=graph.budget.max_wall_seconds,
            )
            accepted = accept_graph(graph, policy)
            run = Run(
                id=run_id,
                goal=Goal(id=f"goal-{run_id}", statement=args.goal),
                accepted_graph=accepted,
                policy=policy,
            )
            store.save_graph(run_id, accepted)
            runtime = DeterministicRuntime(
                {node.id: _declarative_handler for node in graph.nodes}, store=store
            )
            outcome = runtime.execute(run, pause_after_nodes=args.pause_after)
            print(canonical_json({"run_id": run_id, "state": outcome.run.state.value}))
        elif args.command == "inspect":
            print(canonical_json(inspect_run(store, args.run_id)))
        elif args.command == "replay":
            report = DeterministicRuntime({}, store=store).replay(args.run_id)
            print(canonical_json(report.__dict__))
        elif args.command == "resume":
            run = store.get("run", args.run_id, Run)
            handlers = {node.id: _declarative_handler for node in run.accepted_graph.graph.nodes}
            outcome = DeterministicRuntime(handlers, store=store).execute(run, resume=True)
            print(canonical_json({"run_id": args.run_id, "state": outcome.run.state.value}))
        elif args.command in {"pause", "cancel"}:
            store.request_control(args.run_id, args.command)
            print(canonical_json({"run_id": args.run_id, "requested": args.command}))
        elif args.command == "compare":
            print(canonical_json(compare_runs(store, args.left_run_id, args.right_run_id)))
        elif args.command == "serve":
            serve(store, args.host, args.port)
    return 0


def _declarative_handler(context: NodeExecutionContext) -> ResultEnvelope:
    configuration = context.node.configuration
    status_text = (
        configuration.get("status", "succeeded") if isinstance(configuration, dict) else "succeeded"
    )
    status = ResultStatus(status_text)
    value = (
        configuration.get("value")
        if isinstance(configuration, dict) and "value" in configuration
        else _default_value(context)
    )
    return ResultEnvelope(
        contract_id=context.node.output_contract.id,
        status=status,
        value=freeze_json(value),
    )


def _default_value(context: NodeExecutionContext) -> object:
    contract = context.node.output_contract
    if contract.expected_type is ContractKind.OBJECT:
        return {name: True for name in contract.required_fields}
    if contract.expected_type is ContractKind.ARRAY:
        return []
    if contract.expected_type is ContractKind.STRING:
        return ""
    if contract.expected_type is ContractKind.NUMBER:
        return 0
    if contract.expected_type is ContractKind.BOOLEAN:
        return True
    return None
