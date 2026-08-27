"""Command-line interface for the local deterministic Fleet runtime."""

from __future__ import annotations

import argparse
import io
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from . import __version__
from .config import WorkerName, load_operator_config
from .demo import run_demo
from .domain import (
    ContractKind,
    ExecutionPolicy,
    ExecutionStrategy,
    Goal,
    Graph,
    ResultEnvelope,
    ResultStatus,
    RoutingMode,
    Run,
)
from .domain.base import freeze_json
from .domain.policy_v2 import NetworkMode, PolicyLayer, PolicyLayerKind, PolicyResolver
from .domain.services_v2 import Cancellation, WorkerAdapter
from .domain.v2 import (
    AcceptanceLedger,
    ActionKind,
    ActionProposal,
    ApprovalRecord,
    ArtifactDescriptor,
    ArtifactPutRequest,
    PolicyDecision,
    ProcessRequest,
    PromotionRecord,
    WorkspaceSnapshot,
)
from .graph import accept_graph
from .inspector import compare_runs, inspect_any_run, serve
from .project import (
    discover_project,
    discover_project_harness,
    migration_candidate,
    write_migration_candidate,
)
from .routing import assess_task, merge_semantic_assessment, select_strategy
from .runtime import DeterministicRuntime, NodeExecutionContext
from .serialization import canonical_json, loads_yaml_model
from .services_v2 import (
    AtomicArtifactStore,
    DigestApprovalService,
    GitWorkspaceManager,
    LocalProcessExecutor,
    ProjectLocalInstaller,
    RestrictedDownloadClient,
)
from .services_v2._common import identifier, now
from .storage import SQLiteStore
from .worker_adapters import (
    ClaudeCodeCliWorkerAdapter,
    CliTaskAssessmentAdapter,
    CodexCliWorkerAdapter,
    OllamaCliWorkerAdapter,
    ScriptedWorkerAdapter,
    semantic_assessment_schema_json,
    worker_proposal_schema_json,
)


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

    work = commands.add_parser("work", help="create a mediated v0.2 work run")
    work.add_argument("goal")
    work.add_argument("--repo", default=".")
    work.add_argument(
        "--worker",
        choices=("codex_cli", "claude_code_cli", "ollama_cli"),
        default=None,
    )
    work.add_argument("--model", help="explicit worker model (for example qwen3-coder:30b)")
    work.add_argument(
        "--routing-mode",
        choices=("legacy", "fixed", "adaptive"),
        default="adaptive",
        help="selection mode (default: adaptive; legacy enables explicit worker/model use)",
    )
    work.add_argument("--strategy", help="exact strategy ID for fixed routing")
    work.add_argument(
        "--strategy-set",
        help="operator-defined named subset of strategies available to this run",
    )
    work.add_argument(
        "--assessment-strategy",
        help="exact operator-defined strategy used for adaptive task assessment",
    )
    work.add_argument(
        "--operator-config",
        help=(
            "machine-local worker executable config (default: ~/.config/my-ai-employee/config.yaml)"
        ),
    )
    work.add_argument("--plan-only", action="store_true")
    work.add_argument("--non-interactive", action="store_true")
    work.add_argument("--json", action="store_true")
    work.add_argument("--db", default=".fleet/fleet.db")

    approvals = commands.add_parser("approvals", help="manage digest-bound approvals")
    approval_commands = approvals.add_subparsers(dest="approval_command", required=True)
    approval_list = approval_commands.add_parser("list")
    approval_list.add_argument("--run")
    approval_list.add_argument("--db", default=".fleet/fleet.db")
    approval_show = approval_commands.add_parser("show")
    approval_show.add_argument("approval_id")
    approval_show.add_argument("--db", default=".fleet/fleet.db")
    for decision in ("approve", "deny"):
        approval_decide = approval_commands.add_parser(decision)
        approval_decide.add_argument("approval_id")
        approval_decide.add_argument("--request-digest", required=True)
        approval_decide.add_argument("--db", default=".fleet/fleet.db")

    logs = commands.add_parser("logs", help="show durable v0.2 events")
    logs.add_argument("run_id")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--db", default=".fleet/fleet.db")

    diff = commands.add_parser("diff", help="show the exact captured work-run patch")
    diff.add_argument("run_id")
    diff.add_argument("--stat", action="store_true")
    diff.add_argument("--db", default=".fleet/fleet.db")

    promote = commands.add_parser("promote", help="apply an explicitly approved exact patch")
    promote.add_argument("run_id")
    promote.add_argument("--patch-digest", required=True)
    promote.add_argument("--db", default=".fleet/fleet.db")
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
    if args.command == "work":
        return _work(args)
    with SQLiteStore(args.db) as store:
        if args.command == "approvals":
            return _approvals(store, args)
        if args.command == "logs":
            events = store.work_events(args.run_id)
            print(canonical_json({"schema_version": "2", "run_id": args.run_id, "events": events}))
            return 0
        if args.command == "diff":
            return _diff(store, args)
        if args.command == "promote":
            return _promote(store, args)
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
            print(canonical_json(inspect_any_run(store, args.run_id)))
        elif args.command == "replay":
            report = DeterministicRuntime({}, store=store).replay(args.run_id)
            print(canonical_json(report.__dict__))
        elif args.command == "resume":
            try:
                work_run = store.get_work_run(args.run_id)
            except KeyError:
                run = store.get("run", args.run_id, Run)
                handlers = {
                    node.id: _declarative_handler for node in run.accepted_graph.graph.nodes
                }
                outcome = DeterministicRuntime(handlers, store=store).execute(run, resume=True)
                print(canonical_json({"run_id": args.run_id, "state": outcome.run.state.value}))
            else:
                return _resume_work(store, work_run)
        elif args.command in {"pause", "cancel"}:
            store.request_control(args.run_id, args.command)
            print(canonical_json({"run_id": args.run_id, "requested": args.command}))
        elif args.command == "compare":
            print(canonical_json(compare_runs(store, args.left_run_id, args.right_run_id)))
        elif args.command == "serve":
            serve(store, args.host, args.port)
    return 0


def _work(args: argparse.Namespace) -> int:
    from .orchestration import WorkCoordinator, bind_service_decision

    repository = Path(args.repo).resolve()
    run_id = identifier("work")
    routing_enabled = args.routing_mode != "legacy"
    if args.routing_mode == "fixed" and args.strategy is None:
        raise ValueError("--routing-mode fixed requires --strategy")
    if args.routing_mode == "adaptive" and args.strategy is not None:
        raise ValueError("--routing-mode adaptive rejects --strategy")
    if args.routing_mode == "legacy" and args.strategy is not None:
        raise ValueError("--strategy requires --routing-mode fixed")
    if args.routing_mode == "legacy" and args.strategy_set is not None:
        raise ValueError("--strategy-set requires fixed or adaptive routing")
    if args.routing_mode != "adaptive" and args.assessment_strategy is not None:
        raise ValueError("--assessment-strategy requires adaptive routing")
    if routing_enabled and args.model is not None:
        raise ValueError("--routing-mode cannot be combined with --model")
    if routing_enabled and args.worker is not None:
        raise ValueError("--routing-mode cannot be combined with --worker")
    if args.worker == "ollama_cli" and not args.model:
        raise ValueError("--worker ollama_cli requires --model")
    harness = discover_project_harness(repository)
    capabilities = ["edit_intent", "process"]
    if harness.network.mode.value != "disabled":
        capabilities.append("download")
    if harness.install.ecosystems:
        capabilities.append("install")
    operator_config = load_operator_config(args.operator_config)
    worker_name = cast(WorkerName, args.worker or "codex_cli")
    worker_model = args.model
    worker_effort: str | None = None
    task_assessment = None
    assessment_strategy = None
    selected_strategy = None
    effective_strategy_set = None
    routing_mode = None
    strategies: tuple[ExecutionStrategy, ...] = ()
    if routing_enabled:
        routing_mode = RoutingMode(args.routing_mode)
        effective_strategy_set = operator_config.strategy_set_name(args.strategy_set)
        strategies = operator_config.execution_strategies(
            routing_mode, effective_strategy_set
        )
        if not strategies:
            raise ValueError("routing requires operator-configured strategies")
        if not harness.worker.allowed_strategy_ids:
            raise ValueError("routing requires Harness allowed strategy IDs")
        if routing_mode is RoutingMode.ADAPTIVE and not harness.worker.adaptive_routing:
            raise ValueError("adaptive routing requires Harness opt-in")
        risk = (
            6
            if harness.install.ecosystems
            else 3
            if harness.network.mode.value != "disabled"
            else 0
        )
        task_assessment = assess_task(
            args.goal,
            run_id=run_id,
            risk=risk,
            required_capabilities=capabilities,
        )
        if routing_mode is RoutingMode.ADAPTIVE:
            assessment_strategy = operator_config.assessment_strategy(
                routing_mode,
                args.assessment_strategy,
                effective_strategy_set,
            )
            if (
                assessment_strategy.id not in harness.worker.allowed_strategy_ids
                or assessment_strategy.backend not in harness.worker.allowed
                or (
                    assessment_strategy.backend in {"ollama", "ollama_cli"}
                    and not harness.worker.local_backend
                )
            ):
                raise ValueError("assessment strategy is denied by Project Harness")
        else:
            selected_strategy = select_strategy(
                strategies,
                mode=routing_mode,
                fixed_strategy_id=args.strategy,
                assessment=task_assessment,
                allowed_strategy_ids=harness.worker.allowed_strategy_ids,
                allowed_backends=harness.worker.allowed,
                local_backend_allowed=harness.worker.local_backend,
            )
            worker_name = cast(WorkerName, selected_strategy.backend)
            worker_model = selected_strategy.model
            worker_effort = selected_strategy.effort
    worker_command = (
        None if selected_strategy is None and routing_enabled
        else operator_config.worker_command(worker_name)
    )
    assessment_command = (
        None
        if assessment_strategy is None
        else operator_config.worker_command(cast(WorkerName, assessment_strategy.backend))
    )
    db_path = Path(args.db)
    storage_root = db_path.resolve().parent
    workspace_root = repository.parent / f".fleet-{repository.name}" / "workspaces"
    artifacts = AtomicArtifactStore(storage_root / "artifacts")
    descriptors: dict[str, ArtifactDescriptor] = {}
    executable_paths = [Path("/usr/bin"), Path("/bin")]

    def add_executable_path(executable: str) -> None:
        path = Path(executable)
        if path.is_absolute():
            executable_paths.extend((path.parent, path.resolve().parent))
            return
        located = shutil.which(executable)
        if located is not None:
            executable_paths.extend((Path(located).parent, Path(located).resolve().parent))

    for command_config in (worker_command, assessment_command):
        if command_config is None:
            continue
        add_executable_path(command_config.executable)
        for path_entry in command_config.path_entries:
            executable_paths.append(Path(path_entry))
    # Codex and Claude Code are Node-based today. Keep interpreter lookup explicit
    # and deterministic instead of inheriting the host PATH wholesale.
    add_executable_path("node")
    for command in harness.commands.values():
        add_executable_path(command.argv[0])

    with SQLiteStore(db_path) as store:
        def executor_for(root: Path) -> LocalProcessExecutor:
            return LocalProcessExecutor(
                (root,),
                artifacts,
                executable_paths=tuple(dict.fromkeys(executable_paths)),
                inherited_environment={"HOME": str(Path.home())},
                stdin_resolver=lambda digest: artifacts.open_verified(descriptors[digest]),
            )

        def prompt_writer(value: bytes) -> str:
            descriptor = artifacts.put(
                io.BytesIO(value),
                ArtifactPutRequest(
                    id=identifier("worker-prompt"),
                    run_id=run_id,
                    created_at=now(),
                    media_type="application/json",
                    logical_kind="worker_request",
                    producer_action_id=run_id,
                    source=freeze_json({"bounded": True}),
                ),
            )
            descriptors[descriptor.artifact_digest] = descriptor
            store.put("artifact_descriptor_v2", descriptor, run_id=run_id)
            return descriptor.artifact_digest

        def read_output(digest: str) -> bytes:
            path = artifacts.root / "sha256" / digest[:2] / digest
            return path.read_bytes()

        required_approvals = tuple(
            operation
            for operation in (
                "new_dependency",
                "manifest_lock_mutation",
                "lifecycle_scripts",
                "new_registry_domain",
            )
            if getattr(harness.install, operation).value == "approval"
        )
        policy = PolicyLayer(
            id=identifier("builtin-policy"),
            run_id=run_id,
            created_at=now(),
            kind=PolicyLayerKind.BUILTIN,
            allowed_capabilities=tuple(capabilities),
            writable_paths=harness.paths.writable,
            https_domains=harness.network.https_domains,
            network_mode=NetworkMode(harness.network.mode.value),
            process_shell_allowed=False,
            install_ecosystems=harness.install.ecosystems,
            max_wall_seconds=1800.0,
            max_processes=40,
            max_worker_turns=1,
            max_download_bytes=harness.budgets.download_bytes,
            max_artifact_bytes=harness.budgets.artifact_bytes,
            required_approvals=required_approvals,
        )

        def decide_worker_process(request: ProcessRequest) -> PolicyDecision:
            proposal = ActionProposal(
                id=identifier("worker-runtime-proposal"),
                run_id=run_id,
                created_at=now(),
                worker_id="runtime-worker-adapter",
                kind=ActionKind.PROCESS,
                payload=request,
                reason="invoke the selected subscription-authenticated worker CLI",
            )
            resolution = PolicyResolver().resolve(
                proposal,
                (policy,),
                decision_id=identifier("worker-runtime-policy"),
                created_at=now(),
            )
            return bind_service_decision(request, resolution.decision)

        if assessment_strategy is not None:
            assert assessment_command is not None
            assert task_assessment is not None
            assert routing_mode is RoutingMode.ADAPTIVE
            assessment_directory = workspace_root / "assessment" / run_id
            assessment_directory.mkdir(parents=True, exist_ok=True)
            assessment_schema_path: str | None = None
            if assessment_strategy.backend == "codex_cli":
                schema = assessment_directory / "semantic-assessment.json"
                schema.write_bytes(semantic_assessment_schema_json())
                assessment_schema_path = str(schema)
            semantic = CliTaskAssessmentAdapter(
                executor_for(assessment_directory),
                read_output,
                decide_worker_process,
                run_id=run_id,
                strategy=assessment_strategy,
                executable=assessment_command.executable,
                cwd=".",
                prompt_writer=prompt_writer,
                output_schema_path=assessment_schema_path,
                timeout_seconds=harness.budgets.wall_seconds,
            ).assess(
                args.goal,
                task_assessment,
                available_capabilities=capabilities,
            )
            task_assessment = merge_semantic_assessment(
                task_assessment,
                semantic,
                available_capabilities=capabilities,
            )
            selected_strategy = select_strategy(
                strategies,
                mode=routing_mode,
                assessment=task_assessment,
                allowed_strategy_ids=harness.worker.allowed_strategy_ids,
                allowed_backends=harness.worker.allowed,
                local_backend_allowed=harness.worker.local_backend,
            )
            worker_name = cast(WorkerName, selected_strategy.backend)
            worker_model = selected_strategy.model
            worker_effort = selected_strategy.effort
            worker_command = operator_config.worker_command(worker_name)
            add_executable_path(worker_command.executable)
            for path_entry in worker_command.path_entries:
                executable_paths.append(Path(path_entry))

        def worker_factory(
            snapshot: WorkspaceSnapshot | None, cancellation: Cancellation
        ) -> WorkerAdapter:
            root = repository if snapshot is None else Path(snapshot.isolated_worktree)
            assert worker_command is not None
            adapter_type = {
                "codex_cli": CodexCliWorkerAdapter,
                "claude_code_cli": ClaudeCodeCliWorkerAdapter,
                "ollama_cli": OllamaCliWorkerAdapter,
            }[worker_name]
            scratch_directory: str | None = None
            output_schema_path: str | None = None
            if adapter_type is ClaudeCodeCliWorkerAdapter:
                scratch = workspace_root / "worker-scratch" / run_id
                scratch.mkdir(parents=True, exist_ok=True)
                scratch_directory = str(scratch)
            elif adapter_type is CodexCliWorkerAdapter:
                schema_directory = workspace_root / "worker-schema" / run_id
                schema_directory.mkdir(parents=True, exist_ok=True)
                schema = schema_directory / "proposal-envelope.json"
                schema.write_bytes(worker_proposal_schema_json())
                output_schema_path = str(schema)
            return adapter_type(
                executor_for(root),
                read_output,
                decide_worker_process,
                run_id=run_id,
                executable=worker_command.executable,
                prompt_writer=prompt_writer,
                scratch_directory=scratch_directory,
                output_schema_path=output_schema_path,
                model=worker_model,
                effort=worker_effort,
                inherit_environment=("HOME",) if adapter_type is OllamaCliWorkerAdapter else (),
                include_response_schema=adapter_type is OllamaCliWorkerAdapter,
                cancellation=cancellation,
                timeout_seconds=harness.budgets.wall_seconds,
            )

        if harness.provisional or worker_name not in harness.worker.allowed:
            print(
                canonical_json(
                    {
                        "schema_version": "2",
                        "run_id": run_id,
                        "status": "failed",
                        "stable_code": "WORKER_DENIED_BY_HARNESS",
                        "next_actions": (),
                    }
                )
            )
            return 3
        verification_requests = tuple(
            ProcessRequest(
                id=identifier("verification-request"),
                run_id=run_id,
                created_at=now(),
                argv=harness.commands[name].argv,
                cwd=harness.commands[name].cwd,
                inherit_environment=harness.commands[name].inherit_environment,
                timeout_seconds=min(300.0, harness.budgets.wall_seconds),
                budget_class="verification",
                purpose=f"required Harness verification: {name}",
            )
            for name in harness.verification.required
        )
        workspace = GitWorkspaceManager(workspace_root, artifacts)
        coordinator = WorkCoordinator(
            store,
            DeterministicRuntime({}, store=store),
            workspace,
            worker_factory,
            lambda snapshot: executor_for(Path(snapshot.isolated_worktree)),
            lambda descriptor: artifacts.open_verified(descriptor).read(),
            (policy,),
            task_assessment=task_assessment,
            assessment_strategy=assessment_strategy,
            selected_strategy=selected_strategy,
            strategy_set=effective_strategy_set,
            approval_service=DigestApprovalService(store, operator_label="local-operator"),
            download_client=RestrictedDownloadClient(
                artifacts,
                enabled=harness.network.mode.value != "disabled",
                allowed_domains=harness.network.https_domains,
                allowed_ports=harness.network.ports or (443,),
            ),
            installer_factory=lambda snapshot: ProjectLocalInstaller(
                snapshot.isolated_worktree,
                executor_for(Path(snapshot.isolated_worktree)),
                artifacts,
                network_mediated=harness.network.mode.value != "disabled",
            ),
            verification_requests=verification_requests,
            protected_paths=harness.paths.protected,
            allowed_processes=tuple(command.argv for command in harness.commands.values()),
        )
        head = (
            __import__("subprocess")
            .run(
                ("git", "-C", str(repository), "rev-parse", "HEAD"),
                capture_output=True,
                check=True,
                text=True,
            )
            .stdout.strip()
        )
        run = coordinator.start(
            args.goal,
            str(repository),
            head,
            worker_name=worker_name,
            plan_only=args.plan_only,
            run_id=run_id,
        )
        print(
            canonical_json(
                {
                    "schema_version": "2",
                    "run_id": run.id,
                    "status": run.status,
                    "stable_code": run.failure_code,
                    "next_actions": _next_actions(run),
                }
            )
        )
        if run.failure_code and "WORKER" in run.failure_code:
            return 6
        if run.status == "waiting_approval" and args.non_interactive:
            return 4
        return 0 if run.status not in {"failed", "cancelled"} else 5


def _approvals(store: SQLiteStore, args: argparse.Namespace) -> int:
    service = DigestApprovalService(store, operator_label="local-operator")
    if args.approval_command == "list":
        records = store.list_records("approval_v2", ApprovalRecord, run_id=args.run)
        print(canonical_json({"schema_version": "2", "approvals": records}))
    elif args.approval_command == "show":
        record = store.get("approval_v2", args.approval_id, ApprovalRecord)
        print(canonical_json({"schema_version": "2", "approval": record}))
    else:
        decision: Literal["approved", "denied"] = (
            "approved" if args.approval_command == "approve" else "denied"
        )
        record = service.decide(args.approval_id, args.request_digest, decision)
        print(canonical_json({"schema_version": "2", "approval": record}))
    return 0


def _diff(store: SQLiteStore, args: argparse.Namespace) -> int:
    from .domain.v2 import ArtifactDescriptor

    run = store.get_work_run(args.run_id)
    if run.patch_artifact_id is None:
        raise ValueError("run has no captured patch")
    descriptor = store.get("artifact_descriptor_v2", run.patch_artifact_id, ArtifactDescriptor)
    path = Path(descriptor.store_locator)
    state_root = Path(store.path).resolve().parent
    content = (state_root / "artifacts" / path).read_bytes()
    if args.stat:
        print(canonical_json({"schema_version": "2", "run_id": run.id, "bytes": len(content)}))
    else:
        print(content.decode("utf-8", "replace"), end="")
    return 0


def _promote(store: SQLiteStore, args: argparse.Namespace) -> int:
    from .domain.v2 import ArtifactDescriptor

    run = store.get_work_run(args.run_id)
    if run.status == "completed":
        promotions = store.list_records("promotion_v2", PromotionRecord, run_id=run.id)
        if any(item.reviewed_patch_digest == args.patch_digest for item in promotions):
            print(canonical_json({"schema_version": "2", "run_id": run.id, "status": "completed"}))
            return 0
    if run.status != "ready_to_promote" or run.patch_artifact_id is None:
        _print_work_failure(run.id, run.status, "PROMOTION_NOT_READY")
        return 5
    patch = store.get("artifact_descriptor_v2", run.patch_artifact_id, ArtifactDescriptor)
    if patch.artifact_digest != args.patch_digest:
        _print_work_failure(run.id, run.status, "PATCH_DIGEST_MISMATCH")
        return 8
    if (
        run.pending_approval_id is None
        or run.review_digest is None
        or run.acceptance_ledger_id is None
    ):
        _print_work_failure(run.id, run.status, "PROMOTION_APPROVAL_REQUIRED")
        return 4
    approval = store.get("approval_v2", run.pending_approval_id, ApprovalRecord)
    ledger = store.get("acceptance_ledger_v2", run.acceptance_ledger_id, AcceptanceLedger)
    gate_criteria = {
        item.criterion_id: item
        for item in ledger.criteria
        if item.criterion_id in {"reviewed-patch", "promotion-ready"}
    }
    if (
        not ledger.criteria
        or any(item.disposition != "satisfied" for item in ledger.criteria)
        or set(gate_criteria) != {"reviewed-patch", "promotion-ready"}
        or not all(
            patch.artifact_digest in item.evidence_refs and run.review_digest in item.evidence_refs
            for item in gate_criteria.values()
        )
    ):
        _print_work_failure(run.id, run.status, "EVIDENCE_OR_REVIEW_BLOCKED")
        return 5
    if (
        approval.request_digest != patch.artifact_digest
        or approval.policy_digest != run.effective_policy_digest
    ):
        _print_work_failure(run.id, run.status, "STALE_PROMOTION_APPROVAL")
        return 8
    if approval.decision == "pending":
        _print_work_failure(run.id, run.status, "PROMOTION_APPROVAL_REQUIRED")
        return 4
    snapshot = store.get("workspace_v2", run.workspace_id, WorkspaceSnapshot)
    state_root = Path(store.path).resolve().parent
    artifacts = AtomicArtifactStore(state_root / "artifacts")
    workspace = GitWorkspaceManager(Path(snapshot.isolated_worktree).resolve().parent, artifacts)
    workspace.adopt(snapshot)
    try:
        promotion = workspace.promote(snapshot, patch, approval)
    except ValueError:
        _print_work_failure(run.id, run.status, "WORKSPACE_CONFLICT")
        return 8
    store.put("promotion_v2", promotion, run_id=run.id)
    completed = run.model_copy(update={"status": "completed", "generation": run.generation + 1})
    store.save_work_run(completed)
    store.checkpoint_work(
        completed.id,
        completed.generation,
        {
            "status": completed.status,
            "policy_digest": completed.effective_policy_digest,
            "completed_action_digests": completed.completed_action_digests,
        },
    )
    print(canonical_json({"schema_version": "2", "run_id": run.id, "status": "completed"}))
    return 0


def _print_work_failure(run_id: str, status: str, stable_code: str) -> None:
    print(
        canonical_json(
            {
                "schema_version": "2",
                "run_id": run_id,
                "status": status,
                "stable_code": stable_code,
                "next_actions": (),
            }
        )
    )


def _resume_work(store: SQLiteStore, run: object) -> int:
    from .orchestration import WorkCoordinator, WorkRun

    if not isinstance(run, WorkRun):
        raise TypeError("expected a v0.2 work run")
    harness = discover_project_harness(run.repository)
    if run.workspace_id is None:
        print(canonical_json({"schema_version": "2", "run_id": run.id, "status": run.status}))
        return 0
    snapshot = store.get("workspace_v2", run.workspace_id, WorkspaceSnapshot)
    storage_root = Path(store.path).resolve().parent
    artifacts = AtomicArtifactStore(storage_root / "artifacts")
    executable_paths = [Path("/usr/bin"), Path("/bin")]
    for executable in ("codex", "claude"):
        located = shutil.which(executable)
        if located is not None:
            executable_paths.append(Path(located).resolve().parent)
    for command in harness.commands.values():
        located = shutil.which(command.argv[0])
        if located is not None:
            executable_paths.append(Path(located).resolve().parent)

    def executor_for(workspace_snapshot: WorkspaceSnapshot) -> LocalProcessExecutor:
        return LocalProcessExecutor(
            (workspace_snapshot.isolated_worktree,),
            artifacts,
            executable_paths=tuple(dict.fromkeys(executable_paths)),
        )

    policy = PolicyLayer(
        id=identifier("builtin-policy"),
        run_id=run.id,
        created_at=now(),
        kind=PolicyLayerKind.BUILTIN,
        allowed_capabilities=("edit_intent", "process"),
        writable_paths=harness.paths.writable,
        https_domains=(),
        network_mode=NetworkMode.DISABLED,
        process_shell_allowed=False,
        install_ecosystems=(),
        max_wall_seconds=1800.0,
        max_processes=40,
        max_worker_turns=1,
        max_download_bytes=0,
        max_artifact_bytes=16_000_000,
    )
    stored_policies = store.list_records("policy_layer_v2", PolicyLayer, run_id=run.id)
    verification = tuple(
        ProcessRequest(
            id=identifier("verification-request"),
            run_id=run.id,
            created_at=now(),
            argv=harness.commands[name].argv,
            cwd=harness.commands[name].cwd,
            inherit_environment=harness.commands[name].inherit_environment,
            timeout_seconds=min(300.0, harness.budgets.wall_seconds),
            budget_class="verification",
            purpose=f"required Harness verification: {name}",
        )
        for name in harness.verification.required
    )
    coordinator = WorkCoordinator(
        store,
        DeterministicRuntime({}, store=store),
        GitWorkspaceManager(Path(snapshot.isolated_worktree).resolve().parent, artifacts),
        lambda _snapshot, _cancellation: ScriptedWorkerAdapter(()),
        executor_for,
        lambda descriptor: artifacts.open_verified(descriptor).read(),
        stored_policies or (policy,),
        approval_service=DigestApprovalService(store, operator_label="local-operator"),
        download_client=RestrictedDownloadClient(
            artifacts,
            enabled=harness.network.mode.value != "disabled",
            allowed_domains=harness.network.https_domains,
            allowed_ports=harness.network.ports or (443,),
        ),
        installer_factory=lambda workspace_snapshot: ProjectLocalInstaller(
            workspace_snapshot.isolated_worktree,
            executor_for(workspace_snapshot),
            artifacts,
            network_mediated=harness.network.mode.value != "disabled",
        ),
        verification_requests=verification,
        protected_paths=harness.paths.protected,
        allowed_processes=tuple(command.argv for command in harness.commands.values()),
    )
    resumed = coordinator.resume(run.id)
    print(
        canonical_json(
            {
                "schema_version": "2",
                "run_id": resumed.id,
                "status": resumed.status,
                "stable_code": resumed.failure_code,
                "next_actions": _next_actions(resumed),
            }
        )
    )
    return 4 if resumed.status == "waiting_approval" else 0


def _next_actions(run: object) -> tuple[str, ...]:
    status = getattr(run, "status", "failed")
    run_id = getattr(run, "id", "")
    if status == "waiting_approval":
        return (f"fleet approvals list --run {run_id}", f"fleet resume {run_id}")
    if status == "ready_to_promote":
        return (f"fleet diff {run_id}", f"fleet promote {run_id} --patch-digest <digest>")
    return ()


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
