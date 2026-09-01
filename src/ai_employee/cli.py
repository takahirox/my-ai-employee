"""Command-line interface for the local deterministic Fleet runtime."""

from __future__ import annotations

import argparse
import getpass
import io
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from . import __version__
from .config import OperatorConfig, WorkerName, load_operator_config
from .database import DATABASE_ENVIRONMENT_VARIABLE, resolve_database_path
from .demo import run_demo
from .domain import (
    CompletionCriterion,
    ContractKind,
    ExecutionPolicy,
    ExecutionStrategy,
    Goal,
    GoalTaskKind,
    Graph,
    Node,
    ProjectHarnessV2,
    ResultEnvelope,
    ResultStatus,
    RoutingMode,
    Run,
)
from .domain.base import freeze_json
from .domain.browser import BrowserObservation
from .domain.evaluation import (
    EvaluationDecision,
    EvaluationEvidenceLedger,
    EvaluationResult,
    ObservationManifest,
)
from .domain.policy_v2 import NetworkMode, PolicyLayer, PolicyLayerKind, PolicyResolver
from .domain.services_v2 import Cancellation, WorkerAdapter
from .domain.v2 import (
    AcceptanceLedger,
    ActionKind,
    ActionProposal,
    ApprovalRecord,
    ArtifactDescriptor,
    ArtifactPutRequest,
    EditIntentRequest,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
    PromotionRecord,
    WorkerRequest,
    WorkspaceSnapshot,
)
from .eval_framework import (
    EvalEnvironmentSnapshot,
    EvalExperiment,
    EvalReport,
    EvalStrategyBinding,
    EvalTrial,
    deterministic_experiment_id,
    load_scenario_definition,
    resolve_scenario,
    run_experiment,
)
from .graph import GraphValidationError, accept_graph
from .graph_composition import GraphPatchComposer, GraphPatchCompositionRecord
from .graph_evaluation import (
    GraphCandidateEvaluator,
    ParentCandidateEvaluationRecord,
    ParentCandidateEvaluationRequest,
)
from .graph_execution import GraphExecutionService
from .inspector import compare_runs, inspect_any_run, inspect_graph_run, serve
from .parent_review import (
    CliParentSemanticReviewer,
    ParentSemanticReviewDecision,
    ParentSemanticReviewRequest,
    ParentSemanticReviewResult,
    ParentSemanticSeverity,
    decide_parent_semantic_review,
    parent_semantic_review_schema_json,
    validate_parent_semantic_review_result,
)
from .plan_review import (
    CliPlanReviewer,
    PlanReviewGateError,
    plan_review_schema_json,
)
from .project import (
    discover_project,
    discover_project_harness,
    migration_candidate,
    write_migration_candidate,
)
from .promotion_approval import (
    PromotionApprovalTrustKernel,
    PromotionPolicyDecision,
    validate_exact_parent_evidence_store,
    validate_policy_auto_authority,
)
from .routing import assess_task, merge_semantic_profile, select_strategy
from .run_explanation import explain_any_run
from .runtime import DeterministicRuntime, NodeExecutionContext
from .serialization import (
    canonical_digest,
    canonical_json,
    loads_yaml_model,
    operator_config_digest,
    project_harness_digest,
)
from .services_v2 import (
    AtomicArtifactStore,
    DigestApprovalService,
    GitWorkspaceManager,
    LocalProcessExecutor,
    PlaywrightBrowserEvaluationServices,
    ProjectLocalInstaller,
    RestrictedDownloadClient,
)
from .services_v2._common import identifier, now
from .storage import SQLiteStore
from .task_orchestration import (
    GoalEvaluatorRecord,
    GraphRunRecord,
    TaskGraphAcceptance,
    one_node_graph,
)
from .task_planning import (
    CliProposedGraphPlanner,
    PlannerRoutingDecision,
    ProposedGraph,
    proposed_graph_schema_json,
)
from .task_review import (
    CliTaskResultReviewer,
    TaskReviewSeverity,
    task_review_schema_json,
)
from .worker_adapters import (
    ClaudeCodeCliWorkerAdapter,
    CliTaskAssessmentAdapter,
    CodexCliWorkerAdapter,
    OllamaCliWorkerAdapter,
    ScriptedWorkerAdapter,
    cli_inherit_environment,
    semantic_assessment_schema_json,
    worker_proposal_schema_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet", description="My AI Employee fleet runtime")
    parser.add_argument("--version", action="version", version=f"fleet {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    demo = commands.add_parser("demo", help="run the deterministic offline demonstration")
    demo.add_argument("--db")
    demo.add_argument("--run-id", default=None)

    run = commands.add_parser("run", help="run a declarative YAML/JSON graph")
    run.add_argument("graph")
    run.add_argument("--goal", default="Execute the accepted declarative graph")
    run.add_argument("--run-id", default=None)
    run.add_argument("--db")
    run.add_argument("--pause-after", type=int)

    evaluate = commands.add_parser(
        "eval", help="compare fixed Fleet strategies over bounded repeated trials"
    )
    evaluate.add_argument("scenario")
    evaluate.add_argument(
        "--strategy", action="append", required=True, help="exact operator strategy ID"
    )
    evaluate.add_argument("--trials", type=int, default=5)
    evaluate.add_argument("--eval-id", help="explicit experiment ID; otherwise deterministic")
    evaluate.add_argument("--operator-config")
    evaluate.add_argument("--db", default=".fleet/evals.db")
    evaluate.add_argument("--json", action="store_true")

    inspect = commands.add_parser("inspect", help="inspect a persisted run")
    inspect.add_argument("run_id")
    inspect.add_argument("--db")

    explain = commands.add_parser(
        "explain", help="explain one persisted run as a coherent read-only story"
    )
    explain.add_argument("run_id")
    explain.add_argument("--db")

    replay = commands.add_parser("replay", help="replay stored control flow without workers")
    replay.add_argument("run_id")
    replay.add_argument("--db")

    resume = commands.add_parser("resume", help="start a planned run or resume a paused run")
    resume.add_argument("run_id")
    resume.add_argument("--db")

    for name in ("pause", "cancel"):
        control = commands.add_parser(name, help=f"request {name} at the next node boundary")
        control.add_argument("run_id")
        control.add_argument("--db")

    compare = commands.add_parser("compare", help="compare two stored runs and strategies")
    compare.add_argument("left_run_id")
    compare.add_argument("right_run_id")
    compare.add_argument("--db")

    server = commands.add_parser("serve", help="serve the read-only local Inspector")
    server.add_argument("--db")
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
        "--routing-mode",
        choices=("fixed", "adaptive"),
        default="adaptive",
        help="Graph strategy selection mode (default: adaptive)",
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
        "--planner-strategy",
        help="exact planner-eligible strategy used instead of adaptive Planner selection",
    )
    work.add_argument(
        "--operator-config",
        help=(
            "machine-local worker executable config (default: ~/.config/my-ai-employee/config.yaml)"
        ),
    )
    work.add_argument("--plan-only", action="store_true")
    work.add_argument(
        "--task-kind",
        choices=("mutating", "non_mutating"),
        default="mutating",
        help="persisted side-effect contract for the Goal (default: mutating)",
    )
    work.add_argument(
        "--allow-processes",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="authorize declared Harness processes (defaults on only for mutating Goals)",
    )
    work.add_argument(
        "--max-concurrency",
        type=int,
        default=1,
        help="maximum number of accepted task-graph nodes scheduled concurrently",
    )
    work.add_argument("--non-interactive", action="store_true")
    work.add_argument("--json", action="store_true")
    work.add_argument("--db")

    approvals = commands.add_parser("approvals", help="manage digest-bound approvals")
    approval_commands = approvals.add_subparsers(dest="approval_command", required=True)
    approval_list = approval_commands.add_parser("list")
    approval_list.add_argument("--run")
    approval_list.add_argument("--db")
    approval_show = approval_commands.add_parser("show")
    approval_show.add_argument("approval_id")
    approval_show.add_argument("--db")
    for decision in ("approve", "deny"):
        approval_decide = approval_commands.add_parser(decision)
        approval_decide.add_argument("approval_id")
        approval_decide.add_argument("--request-digest", required=True)
        approval_decide.add_argument("--db")

    logs = commands.add_parser("logs", help="show durable v0.2 events")
    logs.add_argument("run_id")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--db")

    diff = commands.add_parser("diff", help="show the exact captured work-run patch")
    diff.add_argument("run_id")
    diff.add_argument("--stat", action="store_true")
    diff.add_argument("--db")

    promote = commands.add_parser("promote", help="apply an explicitly approved exact patch")
    promote.add_argument("run_id")
    promote.add_argument("--patch-digest", required=True)
    promote.add_argument("--db")
    return parser


def _warn_for_explicit_temporary_database(command: str, database_path: str) -> None:
    if command not in {"run", "work"}:
        return
    resolved_path = Path(database_path).resolve()
    temporary_directory = Path(tempfile.gettempdir()).resolve()
    if not resolved_path.is_relative_to(temporary_directory):
        return
    serve_command = shlex.join(("fleet", "serve", "--db", str(resolved_path)))
    print(
        f"warning: temporary database {resolved_path} is absent from the default Inspector; "
        f"run `{serve_command}` to inspect it.",
        file=sys.stderr,
    )


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
    if args.command == "eval":
        return _eval(args)
    database_was_explicitly_selected = (
        args.db is not None or DATABASE_ENVIRONMENT_VARIABLE in os.environ
    )
    args.db = str(resolve_database_path(args.db))
    if database_was_explicitly_selected:
        _warn_for_explicit_temporary_database(args.command, args.db)
    if args.command == "work":
        return _work(args)
    with SQLiteStore(args.db) as store:
        if args.command in {
            "cancel",
            "diff",
            "explain",
            "inspect",
            "logs",
            "pause",
            "promote",
            "resume",
        } and store.is_standalone_work_run(args.run_id):
            raise KeyError(args.run_id)
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
            store.claim_run_id(run_id, Path.cwd())
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
            store.claim_run_id(run_id, Path.cwd())
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
        elif args.command == "explain":
            print(canonical_json(explain_any_run(store, args.run_id)))
        elif args.command == "replay":
            try:
                graph_run = store.get("graph_run_v2", args.run_id, GraphRunRecord)
            except KeyError:
                report = DeterministicRuntime({}, store=store).replay(args.run_id)
                print(canonical_json(report.__dict__))
            else:
                print(
                    canonical_json(
                        {
                            "schema_version": "2",
                            "kind": "graph_replay",
                            "run_id": graph_run.id,
                            "inspection": inspect_graph_run(store, graph_run.id),
                            "worker_invocations": 0,
                            "verification_invocations": 0,
                            "composition_invocations": 0,
                            "promotion_invocations": 0,
                        }
                    )
                )
        elif args.command == "resume":
            try:
                graph_run = store.get("graph_run_v2", args.run_id, GraphRunRecord)
            except KeyError:
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
            else:
                return _resume_graph(store, graph_run)
        elif args.command in {"pause", "cancel"}:
            store.request_control(args.run_id, args.command)
            print(canonical_json({"run_id": args.run_id, "requested": args.command}))
        elif args.command == "compare":
            print(canonical_json(compare_runs(store, args.left_run_id, args.right_run_id)))
        elif args.command == "serve":
            database_path = Path(store.path).resolve()
            print(
                f"Fleet Inspector: http://{args.host}:{args.port} (database: {database_path})",
                flush=True,
            )
            serve(store, args.host, args.port)
    return 0


def _eval(args: argparse.Namespace) -> int:
    definition = load_scenario_definition(args.scenario)
    scenario_path = Path(args.scenario).resolve()
    requested_repository = Path(definition.repository_fixture).expanduser()
    repository = (
        (scenario_path.parent / requested_repository).resolve()
        if not requested_repository.is_absolute()
        else requested_repository.resolve()
    )
    operator_config_path = (
        None
        if args.operator_config is None
        else str(Path(args.operator_config).expanduser().resolve())
    )

    def environment() -> EvalEnvironmentSnapshot:
        status = subprocess.run(
            (
                "git",
                "-C",
                str(repository),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
            capture_output=True,
            check=True,
            text=True,
        ).stdout
        if status:
            raise ValueError("evaluation fixture must have a clean Git status")
        head = subprocess.run(
            ("git", "-C", str(repository), "rev-parse", "HEAD"),
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        current_harness = discover_project_harness(repository)
        current_operator = load_operator_config(operator_config_path)
        return EvalEnvironmentSnapshot(
            repository=str(repository),
            head_commit=head,
            clean_status_digest=canonical_digest(status),
            harness_digest=project_harness_digest(current_harness),
            operator_config_digest=operator_config_digest(current_operator),
        )

    initial_environment = environment()
    harness = discover_project_harness(repository)
    operator_config = load_operator_config(operator_config_path)
    scenario = resolve_scenario(
        definition,
        scenario_path,
        initial_environment,
        harness,
        created_at=now(),
    )
    requested_strategy_ids = tuple(args.strategy)
    if len(requested_strategy_ids) != len(set(requested_strategy_ids)):
        raise ValueError("evaluation strategy IDs must be unique")
    bindings = tuple(
        _eval_strategy_binding(operator_config, harness, strategy_id)
        for strategy_id in requested_strategy_ids
    )
    experiment_id = args.eval_id or deterministic_experiment_id(scenario, bindings, args.trials)
    experiment = EvalExperiment(
        id=experiment_id,
        run_id=experiment_id,
        created_at=now(),
        scenario_id=scenario.id,
        scenario_digest=scenario.content_digest or "",
        strategies=bindings,
        trials_per_strategy=args.trials,
        operator_config_digest=operator_config_digest(operator_config),
    )
    db_path = Path(args.db).expanduser().resolve()
    git_directory = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "--git-dir"),
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    resolved_git_directory = (
        Path(git_directory).resolve()
        if Path(git_directory).is_absolute()
        else (repository / git_directory).resolve()
    )
    if db_path == resolved_git_directory or resolved_git_directory in db_path.parents:
        raise ValueError("evaluation database cannot be stored inside Git metadata")
    artifact_store = AtomicArtifactStore(db_path.parent / "artifacts")

    def execute(trial: EvalTrial, binding: EvalStrategyBinding) -> int:
        work_args = argparse.Namespace(
            goal=scenario.goal,
            repo=scenario.repository,
            routing_mode="fixed",
            strategy=binding.strategy.id,
            strategy_set=binding.strategy_set,
            assessment_strategy=None,
            planner_strategy=None,
            operator_config=operator_config_path,
            plan_only=False,
            max_concurrency=1,
            non_interactive=True,
            json=True,
            db=str(db_path),
            run_id=trial.fleet_run_id,
        )
        with redirect_stdout(io.StringIO()):
            return _work(work_args)

    with SQLiteStore(db_path) as store:
        report = run_experiment(
            store,
            scenario,
            experiment,
            harness,
            environment,
            lambda descriptor: artifact_store.open_verified(descriptor).read(),
            execute,
            clock=now,
        )
    if args.json:
        print(canonical_json(report))
    else:
        _print_eval_summary(report)
    return 0


def _eval_strategy_binding(
    operator_config: OperatorConfig,
    harness: ProjectHarnessV2,
    strategy_id: str,
) -> EvalStrategyBinding:
    if operator_config.routing is None:
        raise ValueError("evaluation requires operator-configured routing strategies")
    routing = operator_config.routing
    configured = tuple(item for item in routing.strategies if item.id == strategy_id)
    if len(configured) != 1:
        raise ValueError(f"unknown or ambiguous evaluation strategy: {strategy_id}")
    containing_sets = tuple(
        sorted(name for name, values in routing.strategy_sets.items() if strategy_id in values)
    )
    if routing.default_strategy_set in containing_sets:
        strategy_set = routing.default_strategy_set
    elif containing_sets:
        strategy_set = containing_sets[0]
    elif routing.default_strategy_set is None:
        strategy_set = None
    else:
        raise ValueError("evaluation strategy is not admitted by any operator strategy set")
    strategies = operator_config.execution_strategies(RoutingMode.FIXED, strategy_set)
    matches = tuple(item for item in strategies if item.id == strategy_id)
    if len(matches) != 1:
        raise ValueError("evaluation strategy resolution is missing or ambiguous")
    strategy = matches[0]
    if (
        strategy.id not in harness.worker.allowed_strategy_ids
        or strategy.backend not in harness.worker.allowed
        or (strategy.backend in {"ollama", "ollama_cli"} and not harness.worker.local_backend)
    ):
        raise ValueError("evaluation strategy is denied by Project Harness")
    return EvalStrategyBinding(
        strategy=strategy,
        strategy_digest=canonical_digest(strategy),
        strategy_set=strategy_set,
    )


def _print_eval_summary(report: EvalReport) -> None:
    print(f"Experiment {report.experiment.id} ({len(report.results)} completed trials)")
    print("Strategy                 Verified      p50 total   Cost")
    for item in report.summaries:
        total = "-" if item.median_total_seconds is None else f"{item.median_total_seconds:.2f}s"
        cost = "-" if item.total_cost is None else f"{item.total_cost:.4f}"
        verified = (
            f"{item.verified_successes}/{item.planned_trials} ({item.verified_success_rate:.0%})"
        )
        print(f"{item.strategy_id:<24} {verified:<13} {total:<11} {cost}")


def _work(args: argparse.Namespace) -> int:
    from .orchestration import WorkCoordinator, bind_service_decision

    resume_run: GraphRunRecord | None = getattr(args, "resume_graph_run", None)
    repository = Path(args.repo).resolve()
    requested_run_id = getattr(args, "run_id", None)
    run_id = (requested_run_id or identifier("work")) if resume_run is None else resume_run.id
    routing_enabled = args.routing_mode in {"fixed", "adaptive"}
    if not routing_enabled:
        raise ValueError("--routing-mode must be fixed or adaptive")
    if args.routing_mode == "fixed" and args.strategy is None:
        raise ValueError("--routing-mode fixed requires --strategy")
    if args.routing_mode == "adaptive" and args.strategy is not None:
        raise ValueError("--routing-mode adaptive rejects --strategy")
    if args.routing_mode != "adaptive" and args.assessment_strategy is not None:
        raise ValueError("--assessment-strategy requires adaptive routing")
    if args.routing_mode != "adaptive" and args.planner_strategy is not None:
        raise ValueError("--planner-strategy requires adaptive routing")
    if args.max_concurrency < 1:
        raise ValueError("--max-concurrency must be positive")
    if args.max_concurrency > 1 and args.routing_mode != "adaptive":
        raise ValueError("task-graph planning requires adaptive routing")
    harness = discover_project_harness(repository)
    try:
        goal = (
            _work_goal(
                run_id,
                args.goal,
                harness,
                task_kind=GoalTaskKind(getattr(args, "task_kind", "mutating")),
                processes_authorized=getattr(args, "allow_processes", None),
            )
            if resume_run is None
            else resume_run.goal
        )
    except ValueError:
        print(
            canonical_json(
                {
                    "schema_version": "2",
                    "run_id": run_id,
                    "status": "failed",
                    "stable_code": "GOAL_CONTRADICTION",
                    "next_actions": (),
                }
            )
        )
        return 2
    if resume_run is not None and project_harness_digest(harness) != resume_run.harness_digest:
        raise ValueError("Project Harness changed since the graph was accepted")
    capabilities: list[str] = []
    if goal.task_kind is GoalTaskKind.MUTATING:
        capabilities.append("edit_intent")
    if goal.processes_authorized:
        capabilities.append("process")
    if goal.task_kind is GoalTaskKind.MUTATING and harness.network.mode.value != "disabled":
        capabilities.append("download")
    if goal.task_kind is GoalTaskKind.MUTATING and harness.install.ecosystems:
        capabilities.append("install")
    operator_config_path = (
        resume_run.operator_config_path
        if resume_run is not None
        else (
            None
            if args.operator_config is None
            else str(Path(args.operator_config).expanduser().resolve())
        )
    )
    if resume_run is not None and operator_config_path is None:
        raise ValueError("authoritative operator configuration cannot be durably recovered")
    operator_config = load_operator_config(operator_config_path)
    if (
        resume_run is not None
        and operator_config_digest(operator_config) != resume_run.operator_config_digest
    ):
        raise ValueError("operator configuration changed since the graph was accepted")
    task_assessment = None
    assessment_strategy = None
    selected_strategy = None
    effective_strategy_set = None
    routing_mode = None
    strategies: tuple[ExecutionStrategy, ...] = ()
    planner_candidates: tuple[ExecutionStrategy, ...] = ()
    planner_strategy: ExecutionStrategy | None = None
    planner_routing: PlannerRoutingDecision | None = None
    proposed_graph: ProposedGraph | None = None
    graph_planner: CliProposedGraphPlanner | None = None
    semantic_assessor: CliTaskAssessmentAdapter | None = None
    risk = 0
    plan_reviewer: CliPlanReviewer | None = None
    task_reviewer: CliTaskResultReviewer | None = None
    task_reviewer_strategy: ExecutionStrategy | None = None
    parent_reviewer: CliParentSemanticReviewer | None = None
    parent_reviewer_strategy: ExecutionStrategy | None = None
    if routing_enabled:
        routing_mode = RoutingMode(args.routing_mode)
        if resume_run is None:
            effective_strategy_set = operator_config.strategy_set_name(args.strategy_set)
            strategies = operator_config.execution_strategies(routing_mode, effective_strategy_set)
            if routing_mode is RoutingMode.ADAPTIVE:
                planner_candidates = operator_config.planner_strategies(
                    routing_mode, effective_strategy_set
                )
        else:
            effective_strategy_set = resume_run.strategy_set
            strategies = resume_run.execution_strategies
        if resume_run is not None:
            task_reviewer_strategy = resume_run.task_reviewer_strategy
            if harness.verification.review.independent_task_review != (
                task_reviewer_strategy is not None
            ):
                raise ValueError("independent task-review configuration changed since acceptance")
        elif harness.verification.review.independent_task_review:
            task_reviewer_strategy = operator_config.task_reviewer_strategy(
                routing_mode, effective_strategy_set
            )
        if harness.verification.review.parent_semantic_review:
            parent_reviewer_strategy = operator_config.parent_reviewer_strategy(
                routing_mode, effective_strategy_set
            )
        if not strategies:
            raise ValueError("routing requires operator-configured strategies")
        if not harness.worker.allowed_strategy_ids:
            raise ValueError("routing requires Harness allowed strategy IDs")
        if task_reviewer_strategy is not None and (
            task_reviewer_strategy not in strategies
            or task_reviewer_strategy.id not in harness.worker.allowed_strategy_ids
            or task_reviewer_strategy.backend not in harness.worker.allowed
            or (
                task_reviewer_strategy.backend in {"ollama", "ollama_cli"}
                and not harness.worker.local_backend
            )
        ):
            raise ValueError("task reviewer is outside Harness/operator routing authority")
        if parent_reviewer_strategy is not None and (
            parent_reviewer_strategy not in strategies
            or parent_reviewer_strategy.id not in harness.worker.allowed_strategy_ids
            or parent_reviewer_strategy.backend not in harness.worker.allowed
            or (
                parent_reviewer_strategy.backend in {"ollama", "ollama_cli"}
                and not harness.worker.local_backend
            )
        ):
            raise ValueError("parent reviewer is outside Harness/operator routing authority")
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
        if routing_mode is RoutingMode.ADAPTIVE and resume_run is None:
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
        elif routing_mode is RoutingMode.ADAPTIVE:
            # Resume uses the persisted per-node strategies and must not reassess
            # the top-level goal or invoke the probabilistic planner again.
            pass
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
    worker_command = (
        None
        if selected_strategy is None
        else operator_config.worker_command(cast(WorkerName, selected_strategy.backend))
    )
    assessment_command = (
        None
        if assessment_strategy is None
        else operator_config.worker_command(cast(WorkerName, assessment_strategy.backend))
    )
    db_path = Path(args.db).expanduser()
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
    for strategy in strategies:
        strategy_command = operator_config.worker_command(cast(WorkerName, strategy.backend))
        add_executable_path(strategy_command.executable)
    for command in harness.commands.values():
        add_executable_path(command.argv[0])

    with SQLiteStore(db_path) as store:
        if resume_run is None:
            store.claim_run_id(run_id, repository)

        def executor_for(root: Path) -> LocalProcessExecutor:
            return LocalProcessExecutor(
                (root,),
                artifacts,
                executable_paths=tuple(dict.fromkeys(executable_paths)),
                inherited_environment={"HOME": str(Path.home()), "USER": getpass.getuser()},
                stdin_resolver=lambda digest: artifacts.open_verified(descriptors[digest]),
            )

        def write_prompt(value: bytes, bound_run_id: str, bound_store: SQLiteStore) -> str:
            descriptor = artifacts.put(
                io.BytesIO(value),
                ArtifactPutRequest(
                    id=identifier("worker-prompt"),
                    run_id=bound_run_id,
                    created_at=now(),
                    media_type="application/json",
                    logical_kind="worker_request",
                    producer_action_id=bound_run_id,
                    source=freeze_json({"bounded": True}),
                ),
            )
            descriptors[descriptor.artifact_digest] = descriptor
            bound_store.put("artifact_descriptor_v2", descriptor, run_id=bound_run_id)
            return descriptor.artifact_digest

        def prompt_writer(value: bytes) -> str:
            return write_prompt(value, run_id, store)

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
        if resume_run is None:
            store.put("policy_layer_v2", policy, run_id=run_id)
        else:
            persisted_layers = tuple(
                layer
                for layer in store.list_records("policy_layer_v2", PolicyLayer)
                if canonical_digest((layer.content_digest,)) == resume_run.effective_policy_digest
            )
            if len(persisted_layers) != 1:
                raise ValueError("authoritative graph policy is missing or ambiguous")
            policy = persisted_layers[0]

        def resolve_service_request(
            request: ProcessRequest | EditIntentRequest, kind: ActionKind
        ) -> PolicyDecision:
            proposal = ActionProposal(
                id=identifier("worker-runtime-proposal"),
                run_id=request.run_id,
                created_at=now(),
                worker_id="runtime-worker-adapter",
                kind=kind,
                payload=request,
                reason="invoke an existing policy-mediated graph service boundary",
            )
            resolution = PolicyResolver().resolve(
                proposal,
                (policy,),
                decision_id=identifier("worker-runtime-policy"),
                created_at=now(),
            )
            effective_decision = resolution.decision.model_copy(
                update={
                    "effective_policy_digest": canonical_digest((policy.content_digest,)),
                    "content_digest": None,
                }
            )
            return bind_service_decision(request, effective_decision)

        def decide_worker_process(request: ProcessRequest) -> PolicyDecision:
            return resolve_service_request(request, ActionKind.PROCESS)

        if task_reviewer_strategy is not None:
            review_directory = workspace_root / "assessment" / run_id
            review_directory.mkdir(parents=True, exist_ok=True)
            task_review_schema_path: str | None = None
            if task_reviewer_strategy.backend == "codex_cli":
                task_review_schema = review_directory / "task-review.json"
                task_review_schema.write_bytes(task_review_schema_json())
                task_review_schema_path = str(task_review_schema)
            task_reviewer_command = operator_config.worker_command(
                cast(WorkerName, task_reviewer_strategy.backend)
            )
            task_reviewer = CliTaskResultReviewer(
                executor_for(review_directory),
                read_output,
                decide_worker_process,
                run_id=run_id,
                strategy=task_reviewer_strategy,
                executable=task_reviewer_command.executable,
                cwd=".",
                prompt_writer=prompt_writer,
                output_schema_path=task_review_schema_path,
                timeout_seconds=harness.budgets.wall_seconds,
            )

        if parent_reviewer_strategy is not None:
            review_directory = workspace_root / "assessment" / run_id
            review_directory.mkdir(parents=True, exist_ok=True)
            parent_review_schema_path: str | None = None
            if parent_reviewer_strategy.backend == "codex_cli":
                parent_review_schema = review_directory / "parent-semantic-review.json"
                parent_review_schema.write_bytes(parent_semantic_review_schema_json())
                parent_review_schema_path = str(parent_review_schema)
            parent_reviewer_command = operator_config.worker_command(
                cast(WorkerName, parent_reviewer_strategy.backend)
            )
            parent_reviewer = CliParentSemanticReviewer(
                executor_for(review_directory),
                read_output,
                lambda descriptor: artifacts.open_verified(descriptor).read(),
                decide_worker_process,
                run_id=run_id,
                strategy=parent_reviewer_strategy,
                executable=parent_reviewer_command.executable,
                cwd=".",
                prompt_writer=prompt_writer,
                output_schema_path=parent_review_schema_path,
                timeout_seconds=harness.budgets.wall_seconds,
                maximum_candidate_bytes=min(1_000_000, harness.budgets.artifact_bytes),
            )

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
            semantic_assessor = CliTaskAssessmentAdapter(
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
                expected_effective_policy_digest=canonical_digest((policy.content_digest,)),
            )
            semantic = semantic_assessor.assess(args.goal, task_assessment)
            task_assessment = merge_semantic_profile(
                task_assessment,
                semantic,
            )
            candidates = tuple(sorted(planner_candidates, key=lambda item: item.id))
            allowed_ids = set(harness.worker.allowed_strategy_ids)
            allowed_backends = set(harness.worker.allowed)
            required = set(task_assessment.required_capabilities)
            eligible_planners = tuple(
                item
                for item in candidates
                if item.id in allowed_ids
                and item.backend in allowed_backends
                and (item.backend not in {"ollama", "ollama_cli"} or harness.worker.local_backend)
                and required <= set(item.capabilities)
                and task_assessment.risk <= item.max_risk
                and item.min_complexity <= task_assessment.complexity <= item.max_complexity
                and item.min_scale <= task_assessment.scale <= item.max_scale
            )
            if not eligible_planners:
                raise ValueError("no explicitly configured Planner satisfies routing constraints")
            planner_selection_mode = (
                RoutingMode.FIXED if args.planner_strategy is not None else RoutingMode.ADAPTIVE
            )
            selected_planner = select_strategy(
                eligible_planners,
                mode=planner_selection_mode,
                fixed_strategy_id=args.planner_strategy,
                assessment=task_assessment,
                allowed_strategy_ids=tuple(item.id for item in eligible_planners),
                allowed_backends=tuple(dict.fromkeys(item.backend for item in eligible_planners)),
                local_backend_allowed=harness.worker.local_backend,
            )
            harness_digest = project_harness_digest(harness)
            effective_policy_digest = canonical_digest((policy.content_digest,))
            planner_routing = PlannerRoutingDecision(
                selection_mode=planner_selection_mode,
                strategy_set=effective_strategy_set,
                assessment_strategy=assessment_strategy,
                assessment=task_assessment,
                assessment_digest=canonical_digest(task_assessment),
                candidate_strategy_ids=tuple(item.id for item in candidates),
                eligible_strategy_ids=tuple(item.id for item in eligible_planners),
                selected_strategy=selected_planner,
                effective_policy_digest=effective_policy_digest,
                harness_digest=harness_digest,
                operator_config_digest=operator_config_digest(operator_config),
            )
            planner_strategy = selected_planner.model_copy(update={"routing_reasons": ()})
            planner_command = operator_config.worker_command(
                cast(WorkerName, planner_strategy.backend)
            )
            selected_strategy = select_strategy(
                strategies,
                mode=routing_mode,
                assessment=task_assessment,
                allowed_strategy_ids=harness.worker.allowed_strategy_ids,
                allowed_backends=harness.worker.allowed,
                local_backend_allowed=harness.worker.local_backend,
            )
            worker_command = operator_config.worker_command(
                cast(WorkerName, selected_strategy.backend)
            )
            planner_schema_path: str | None = None
            if planner_strategy.backend == "codex_cli":
                planner_schema = assessment_directory / "proposed-graph.json"
                planner_schema.write_bytes(proposed_graph_schema_json())
                planner_schema_path = str(planner_schema)
                reviewer_schema = assessment_directory / "plan-review.json"
                reviewer_schema.write_bytes(plan_review_schema_json())
                reviewer_schema_path: str | None = str(reviewer_schema)
            else:
                reviewer_schema_path = None
            try:
                graph_planner = CliProposedGraphPlanner(
                    executor_for(assessment_directory),
                    read_output,
                    decide_worker_process,
                    run_id=run_id,
                    strategy=planner_strategy,
                    executable=planner_command.executable,
                    cwd=".",
                    prompt_writer=prompt_writer,
                    output_schema_path=planner_schema_path,
                    timeout_seconds=harness.budgets.wall_seconds,
                    planner_routing=planner_routing,
                )
                plan_reviewer = CliPlanReviewer(
                    executor_for(assessment_directory),
                    read_output,
                    decide_worker_process,
                    run_id=run_id,
                    strategy=planner_strategy,
                    executable=planner_command.executable,
                    cwd=".",
                    prompt_writer=prompt_writer,
                    output_schema_path=reviewer_schema_path,
                    timeout_seconds=harness.budgets.wall_seconds,
                )
                proposed_graph = graph_planner.plan(
                    goal,
                    available_capabilities=tuple(capabilities),
                    effective_policy_digest=effective_policy_digest,
                    harness_digest=harness_digest,
                    max_nodes=16,
                    max_wall_seconds=min(1800.0, harness.budgets.wall_seconds),
                )
            except ValueError:
                print(
                    canonical_json(
                        {
                            "schema_version": "2",
                            "run_id": run_id,
                            "status": "failed",
                            "stable_code": "GRAPH_PLANNER_FAILED",
                            "next_actions": (),
                        }
                    )
                )
                return 7
            add_executable_path(worker_command.executable)
            for path_entry in worker_command.path_entries:
                executable_paths.append(Path(path_entry))

        def build_worker_adapter(
            snapshot: WorkspaceSnapshot | None,
            cancellation: Cancellation,
            *,
            bound_run_id: str,
            bound_worker_name: WorkerName,
            bound_model: str | None,
            bound_effort: str | None,
            bound_store: SQLiteStore,
        ) -> WorkerAdapter:
            root = repository if snapshot is None else Path(snapshot.isolated_worktree)
            command = operator_config.worker_command(bound_worker_name)
            adapter_type = {
                "codex_cli": CodexCliWorkerAdapter,
                "claude_code_cli": ClaudeCodeCliWorkerAdapter,
                "ollama_cli": OllamaCliWorkerAdapter,
            }[bound_worker_name]
            scratch_directory: str | None = None
            output_schema_path: str | None = None
            if adapter_type is ClaudeCodeCliWorkerAdapter:
                scratch = workspace_root / "worker-scratch" / bound_run_id
                scratch.mkdir(parents=True, exist_ok=True)
                scratch_directory = str(scratch)
            elif adapter_type is CodexCliWorkerAdapter:
                schema_directory = workspace_root / "worker-schema" / bound_run_id
                schema_directory.mkdir(parents=True, exist_ok=True)
                schema = schema_directory / "proposal-envelope.json"
                schema.write_bytes(worker_proposal_schema_json())
                output_schema_path = str(schema)
            return adapter_type(
                executor_for(root),
                read_output,
                decide_worker_process,
                run_id=bound_run_id,
                executable=command.executable,
                prompt_writer=lambda value: write_prompt(value, bound_run_id, bound_store),
                scratch_directory=scratch_directory,
                output_schema_path=output_schema_path,
                model=bound_model,
                effort=bound_effort,
                inherit_environment=cli_inherit_environment(bound_worker_name),
                include_response_schema=adapter_type is OllamaCliWorkerAdapter,
                cancellation=cancellation,
                timeout_seconds=harness.budgets.wall_seconds,
            )

        if harness.provisional:
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
        workspace = GitWorkspaceManager(workspace_root, artifacts)
        harness_digest = project_harness_digest(harness)
        operator_digest = operator_config_digest(operator_config)
        effective_policy_digest = canonical_digest((policy.content_digest,))
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
        if resume_run is not None and head != resume_run.base_commit:
            raise ValueError("repository HEAD changed since the graph was accepted")
        if routing_enabled:
            execution_strategies = (
                strategies if routing_mode is RoutingMode.ADAPTIVE else (selected_strategy,)
            )
            assert all(item is not None for item in execution_strategies)

            def coordinator_factory(
                node: Node,
                request: WorkerRequest,
                strategy: ExecutionStrategy,
            ) -> WorkCoordinator:
                inner_store = SQLiteStore(db_path)
                node_worker_name = cast(WorkerName, strategy.backend)
                node_verification_names = tuple(
                    name
                    for name in harness.verification.required
                    if any(
                        name in criterion.verification_requirement_ids
                        for criterion in node.completion_criteria
                    )
                )
                node_verification_requests = (
                    tuple(
                        ProcessRequest(
                            id=identifier("node-verification-request"),
                            run_id=request.run_id,
                            created_at=now(),
                            argv=harness.commands[name].argv,
                            cwd=harness.commands[name].cwd,
                            inherit_environment=harness.commands[name].inherit_environment,
                            timeout_seconds=min(300.0, harness.budgets.wall_seconds),
                            budget_class="verification",
                            purpose=f"node candidate Harness verification: {name}",
                        )
                        for name in node_verification_names
                    )
                    if (
                        goal.task_kind is GoalTaskKind.MUTATING
                        and "edit_intent" in node.required_capabilities
                    )
                    else ()
                )
                node_assessment = assess_task(
                    node.objective or node.name,
                    run_id=request.run_id,
                    risk=node.risk,
                    required_capabilities=node.required_capabilities,
                ).model_copy(
                    update={
                        "complexity": node.complexity,
                        "scale": node.scale,
                        "semantic_profile": node.semantic_profile,
                    }
                )
                return WorkCoordinator(
                    inner_store,
                    DeterministicRuntime({}, store=inner_store),
                    GitWorkspaceManager(workspace_root, artifacts),
                    lambda snapshot, cancellation: build_worker_adapter(
                        snapshot,
                        cancellation,
                        bound_run_id=request.run_id,
                        bound_worker_name=node_worker_name,
                        bound_model=strategy.model,
                        bound_effort=strategy.effort,
                        bound_store=inner_store,
                    ),
                    lambda snapshot: executor_for(Path(snapshot.isolated_worktree)),
                    lambda descriptor: artifacts.open_verified(descriptor).read(),
                    (policy,),
                    task_assessment=node_assessment,
                    selected_strategy=strategy,
                    strategy_set=effective_strategy_set,
                    request_promotion_approval=False,
                    approval_service=DigestApprovalService(
                        inner_store, operator_label="local-operator"
                    ),
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
                    max_worker_turns=max(1, node.resource_budget.worker_turns),
                    verification_requests=node_verification_requests,
                    protected_paths=harness.paths.protected,
                    allowed_processes=tuple(command.argv for command in harness.commands.values()),
                    artifact_store=artifacts,
                )

            def decide_composition(edit: EditIntentRequest) -> PolicyDecision:
                return resolve_service_request(edit, ActionKind.EDIT_INTENT)

            declared_parent_processes = {command.argv for command in harness.commands.values()}

            def decide_parent_process(request: ProcessRequest) -> PolicyDecision:
                if request.argv not in declared_parent_processes:
                    raise ValueError("parent process is not declared by Project Harness")
                return decide_worker_process(request)

            composer = GraphPatchComposer(store, workspace, artifacts, decide_composition)
            parent_evaluator = GraphCandidateEvaluator(
                store,
                workspace,
                harness,
                lambda snapshot: executor_for(Path(snapshot.isolated_worktree)),
                decide_parent_process,
                browser_services_factory=lambda snapshot, cancellation: (
                    PlaywrightBrowserEvaluationServices(
                        snapshot.isolated_worktree,
                        artifacts,
                        cancellation,
                        maximum_artifact_bytes=min(8_000_000, harness.budgets.artifact_bytes),
                    )
                ),
                semantic_reviewer=parent_reviewer,
            )
            fixed_strategy_id = None
            if routing_mode is RoutingMode.FIXED:
                assert selected_strategy is not None
                fixed_strategy_id = selected_strategy.id
            service = GraphExecutionService(
                store,
                coordinator_factory,
                composer,
                cast(tuple[ExecutionStrategy, ...], execution_strategies),
                repository=str(repository),
                base_commit=head,
                max_concurrency=args.max_concurrency,
                routing_mode=routing_mode,
                fixed_strategy_id=fixed_strategy_id,
                allowed_strategy_ids=harness.worker.allowed_strategy_ids,
                allowed_backends=harness.worker.allowed,
                local_backend_allowed=harness.worker.local_backend,
                parent_evaluator=parent_evaluator,
                approval_service=DigestApprovalService(store, operator_label="local-operator"),
                promotion_approval_policy=(
                    PromotionApprovalTrustKernel(
                        harness,
                        operator_config.promotion_auto_approval,
                        harness_digest=harness_digest,
                        operator_config_digest=operator_digest,
                    )
                    if (
                        operator_config.promotion_auto_approval.mode == "policy"
                        or harness.approvals.promotion == "policy"
                    )
                    else None
                ),
                operator_config_digest=operator_digest,
                operator_config_path=operator_config_path,
                strategy_set=effective_strategy_set,
                plan_reviewer=plan_reviewer,
                plan_reviser=graph_planner,
                node_assessor=semantic_assessor,
                routing_risk_floor=risk,
                independent_node_assessment=routing_mode is RoutingMode.ADAPTIVE,
                task_reviewer=task_reviewer,
                independent_task_review=task_reviewer is not None,
                task_review_block_severities=tuple(
                    TaskReviewSeverity(item)
                    for item in harness.verification.review.block_severities
                ),
            )
            graph_input: Graph | ProposedGraph
            if resume_run is not None:
                graph_input = store.list_records(
                    "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run_id
                )[-1].accepted_revision.graph
                harness_digest = resume_run.harness_digest
                effective_policy_digest = resume_run.effective_policy_digest
            elif proposed_graph is not None:
                graph_input = proposed_graph
                harness_digest = proposed_graph.harness_digest
                effective_policy_digest = proposed_graph.effective_policy_digest
            else:
                node_goal = Goal(
                    id=f"node-goal-{run_id}",
                    statement=args.goal,
                    completion_criteria=(
                        CompletionCriterion(
                            id=f"node-patch-{run_id}",
                            description="the node produced an exact mediated workspace patch",
                            required_artifact_ids=("workspace_patch",),
                        ),
                    ),
                )
                graph_input = one_node_graph(
                    node_goal,
                    graph_id=f"graph-{run_id}",
                    node_id=f"node-{run_id}",
                    required_capabilities=tuple(capabilities),
                    max_wall_seconds=min(1800.0, harness.budgets.wall_seconds),
                )
            try:
                graph_run = service.run(
                    goal,
                    graph_input,
                    (
                        resume_run.execution_policy
                        if resume_run is not None
                        else ExecutionPolicy(
                            max_nodes=16,
                            max_attempts=32,
                            max_wall_seconds=min(1800.0, harness.budgets.wall_seconds),
                        )
                    ),
                    harness_digest=harness_digest,
                    effective_policy_digest=effective_policy_digest,
                    run_id=run_id,
                    available_capabilities=tuple(capabilities),
                    plan_only=args.plan_only,
                    resume=resume_run is not None,
                )
            except PlanReviewGateError as error:
                print(
                    canonical_json(
                        {
                            "schema_version": "2",
                            "run_id": run_id,
                            "status": "failed",
                            "stable_code": error.stable_code,
                            "next_actions": (),
                        }
                    )
                )
                return 7
            except GraphValidationError:
                print(
                    canonical_json(
                        {
                            "schema_version": "2",
                            "run_id": run_id,
                            "status": "failed",
                            "stable_code": "GRAPH_PLAN_REJECTED",
                            "next_actions": (),
                        }
                    )
                )
                return 7
            print(
                canonical_json(
                    {
                        "schema_version": "2",
                        "run_id": graph_run.id,
                        "status": graph_run.status,
                        "stable_code": graph_run.failure_code,
                        "next_actions": (),
                    }
                )
            )
            return 0 if graph_run.status in {"planned", "completed", "ready_to_promote"} else 5


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
    try:
        graph_run = store.get("graph_run_v2", args.run_id, GraphRunRecord)
    except KeyError:
        run = store.get_work_run(args.run_id)
        if run.patch_artifact_id is None:
            raise ValueError("run has no captured patch") from None
        run_id = run.id
        descriptor = store.get("artifact_descriptor_v2", run.patch_artifact_id, ArtifactDescriptor)
    else:
        if graph_run.status == "completed" and graph_run.composition_id is None:
            print(
                canonical_json(
                    {
                        "schema_version": "2",
                        "run_id": graph_run.id,
                        "status": graph_run.status,
                        "stable_code": "PATCHLESS_RUN_HAS_NO_DIFF",
                    }
                )
            )
            return 5
        run_id = graph_run.id
        descriptor, _, _ = _graph_candidate(store, graph_run)
    state_root = Path(store.path).resolve().parent
    artifacts = AtomicArtifactStore(state_root / "artifacts")
    with artifacts.open_verified(descriptor) as stream:
        content = stream.read()
    if args.stat:
        print(canonical_json({"schema_version": "2", "run_id": run_id, "bytes": len(content)}))
    else:
        print(content.decode("utf-8", "replace"), end="")
    return 0


def _promote(store: SQLiteStore, args: argparse.Namespace) -> int:
    try:
        graph_run = store.get("graph_run_v2", args.run_id, GraphRunRecord)
    except KeyError:
        return _promote_work(store, args)
    return _promote_graph(store, graph_run, args.patch_digest)


def _promote_work(store: SQLiteStore, args: argparse.Namespace) -> int:

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


def _graph_candidate(
    store: SQLiteStore, run: GraphRunRecord
) -> tuple[ArtifactDescriptor, GraphPatchCompositionRecord, WorkspaceSnapshot]:
    if (
        run.composition_id is None
        or run.composition_digest is None
        or run.parent_candidate_artifact_id is None
        or run.parent_candidate_digest is None
    ):
        raise ValueError("graph candidate bindings are incomplete")
    composition = store.get(
        "graph_patch_composition_v2", run.composition_id, GraphPatchCompositionRecord
    )
    if (
        composition.content_digest != run.composition_digest
        or composition.status != "succeeded"
        or composition.candidate_patch is None
        or composition.composition_workspace is None
        or composition.candidate_patch.id != run.parent_candidate_artifact_id
        or composition.candidate_patch.artifact_digest != run.parent_candidate_digest
    ):
        raise ValueError("graph composition is missing, failed, or stale")
    descriptor = store.get(
        "artifact_descriptor_v2", run.parent_candidate_artifact_id, ArtifactDescriptor
    )
    workspace = store.get("workspace_v2", composition.composition_workspace.id, WorkspaceSnapshot)
    if descriptor != composition.candidate_patch or workspace != composition.composition_workspace:
        raise ValueError("graph candidate descriptor or workspace is stale")
    return descriptor, composition, workspace


def _graph_promotion_evidence(
    store: SQLiteStore,
    run: GraphRunRecord,
    patch: ArtifactDescriptor,
    composition: GraphPatchCompositionRecord,
    workspace: WorkspaceSnapshot,
) -> tuple[ParentCandidateEvaluationRecord, str, tuple[str, ...]]:
    acceptances = tuple(
        item
        for item in store.list_records(
            "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run.id
        )
        if item.accepted_revision.content_digest == run.accepted_graph_revision_digest
    )
    if len(acceptances) != 1:
        raise ValueError("graph acceptance is missing or ambiguous")
    policy_digest = acceptances[0].effective_policy_digest
    if run.parent_evaluation_id is None or run.parent_evaluation_digest is None:
        raise ValueError("parent evaluation binding is missing")
    evaluation = store.get(
        "parent_candidate_evaluation_v2",
        run.parent_evaluation_id,
        ParentCandidateEvaluationRecord,
    )
    if (
        evaluation.content_digest != run.parent_evaluation_digest
        or evaluation.status != "ready_to_promote"
        or evaluation.decision is not EvaluationDecision.PASS
        or evaluation.accepted_graph_revision_digest != run.accepted_graph_revision_digest
        or evaluation.composition_record_digest != composition.content_digest
        or evaluation.composition_workspace_digest != workspace.content_digest
        or evaluation.candidate_descriptor_digest != patch.content_digest
        or evaluation.candidate_artifact_digest != patch.artifact_digest
        or evaluation.effective_policy_digest != policy_digest
        or not evaluation.verification_result_digests
        or not evaluation.evaluation_ledger_digests
    ):
        raise ValueError("parent evaluation is not an exact authoritative PASS")
    requests = tuple(
        item
        for item in store.list_records(
            "parent_candidate_evaluation_request_v2",
            ParentCandidateEvaluationRequest,
            run_id=run.id,
        )
        if item.content_digest == evaluation.request_digest
    )
    if len(requests) != 1:
        raise ValueError("parent evaluation request is missing or ambiguous")
    request = requests[0]
    if (
        request.composition_id != composition.id
        or request.composition_record_digest != composition.content_digest
        or request.composition_workspace != workspace
        or request.candidate_artifact != patch
        or request.effective_policy_digest != policy_digest
        or tuple(
            item.process_request.content_digest
            for item in request.verification_bindings
            if item.process_request is not None
        )
        != evaluation.verification_request_digests
    ):
        raise ValueError("parent evaluation request bindings are stale")
    ledger_digests = {
        item.content_digest
        for item in store.list_records(
            "evaluation_evidence_ledger_v2",
            EvaluationEvidenceLedger,
            run_id=run.id,
        )
        if item.decision is EvaluationDecision.PASS
    }
    verification_digests = {
        item.content_digest
        for item in store.list_records("verification_result_v2", ExecutionResult, run_id=run.id)
        if item.status == "succeeded"
    }
    browser_observation_digests = {
        item.content_digest
        for item in store.list_records("browser_observation_v2", BrowserObservation, run_id=run.id)
        if item.status == "succeeded"
    }
    if (
        not set(evaluation.evaluation_ledger_digests) <= ledger_digests
        or not set(evaluation.verification_result_digests)
        <= verification_digests | browser_observation_digests
    ):
        raise ValueError("parent PASS evidence is missing")
    goal_evaluations = tuple(
        item
        for item in store.list_records("goal_evaluator_v2", GoalEvaluatorRecord, run_id=run.id)
        if item.content_digest == evaluation.goal_evaluator_digest
    )
    if len(goal_evaluations) != 1 or goal_evaluations[0].decision is not EvaluationDecision.PASS:
        raise ValueError("parent goal PASS evidence is missing or stale")
    goal_evaluation = goal_evaluations[0]
    acceptance_ledgers = tuple(
        item
        for item in store.list_records("acceptance_ledger_v2", AcceptanceLedger, run_id=run.id)
        if item.content_digest in goal_evaluation.evidence_digests
    )
    if len(acceptance_ledgers) != 1:
        raise ValueError("parent AcceptanceLedger is missing or stale")
    acceptance_ledger = acceptance_ledgers[0]
    deterministic_prefix = (
        acceptance_ledger.content_digest,
        *evaluation.evaluation_ledger_digests,
    )
    if (
        acceptance_ledger.content_digest is None
        or goal_evaluation.evidence_digests[: len(deterministic_prefix)] != deterministic_prefix
    ):
        raise ValueError("parent AcceptanceLedger is missing or stale")
    semantic_evidence = goal_evaluation.evidence_digests[len(deterministic_prefix) :]
    if run.repository is None:
        raise ValueError("graph repository authority is missing")
    harness = discover_project_harness(run.repository)
    accepted_semantic_findings: tuple[str, ...] = ()
    if harness.verification.review.parent_semantic_review:
        semantic_set = set(semantic_evidence)
        semantic_requests = tuple(
            item
            for item in store.list_records(
                "parent_semantic_review_request_v2", ParentSemanticReviewRequest, run_id=run.id
            )
            if item.content_digest in semantic_set
        )
        semantic_results = tuple(
            item
            for item in store.list_records(
                "parent_semantic_review_result_v2", ParentSemanticReviewResult, run_id=run.id
            )
            if item.content_digest in semantic_set
        )
        semantic_decisions = tuple(
            item
            for item in store.list_records(
                "parent_semantic_review_decision_v2", ParentSemanticReviewDecision, run_id=run.id
            )
            if item.content_digest in semantic_set
        )
        if (
            len(semantic_requests) != 1
            or len(semantic_results) != 1
            or len(semantic_decisions) != 1
        ):
            raise ValueError("parent semantic PASS evidence is missing or ambiguous")
        semantic_request = semantic_requests[0]
        semantic_result = semantic_results[0]
        semantic_decision = semantic_decisions[0]
        validate_parent_semantic_review_result(semantic_request, semantic_result)
        expected_semantic_decision = decide_parent_semantic_review(
            semantic_request,
            semantic_result,
            block_severities=tuple(
                ParentSemanticSeverity(item)
                for item in harness.verification.review.block_severities
            ),
            decision_id=semantic_decision.id,
            run_id=semantic_decision.run_id,
            created_at=semantic_decision.created_at,
        )
        expected_semantic = (
            semantic_request.content_digest,
            semantic_result.content_digest,
            semantic_decision.content_digest,
        )
        if (
            semantic_decision != expected_semantic_decision
            or semantic_decision.action is not EvaluationDecision.PASS
            or semantic_evidence != expected_semantic
        ):
            raise ValueError("parent semantic PASS evidence is stale")
        accepted_semantic_findings = semantic_decision.accepted_finding_digests
    elif semantic_evidence:
        raise ValueError("unexpected parent semantic evidence")
    if tuple(item.criterion_id for item in acceptance_ledger.criteria) != tuple(
        item.id for item in request.goal.completion_criteria
    ) or any(item.disposition != "satisfied" for item in acceptance_ledger.criteria):
        raise ValueError("parent AcceptanceLedger does not satisfy the exact Goal")
    evaluation_result_digests = {
        item.content_digest
        for item in store.list_records("evaluation_result_v2", EvaluationResult, run_id=run.id)
    }
    observation_manifest_digests = {
        item.content_digest
        for item in store.list_records(
            "observation_manifest_v2", ObservationManifest, run_id=run.id
        )
    }
    artifact_digests = {
        digest
        for item in store.list_records("artifact_descriptor_v2", ArtifactDescriptor, run_id=run.id)
        for digest in (item.content_digest, item.artifact_digest)
        if digest is not None
    }
    authoritative_evidence = {
        *evaluation.evaluation_ledger_digests,
        *evaluation_result_digests,
        *observation_manifest_digests,
        *artifact_digests,
        *semantic_evidence,
    }
    authoritative_evidence.update(accepted_semantic_findings)
    if any(
        not item.evidence_refs or not set(item.evidence_refs) <= authoritative_evidence
        for item in acceptance_ledger.criteria
    ):
        raise ValueError("parent AcceptanceLedger cites non-authoritative evidence")
    return evaluation, policy_digest, semantic_evidence


def _promote_graph(store: SQLiteStore, run: GraphRunRecord, patch_digest: str) -> int:
    promotions = store.list_records("promotion_v2", PromotionRecord, run_id=run.id)
    if run.status == "completed" and any(
        item.reviewed_patch_digest == patch_digest for item in promotions
    ):
        print(canonical_json({"schema_version": "2", "run_id": run.id, "status": "completed"}))
        return 0
    if run.status == "completed" and run.composition_id is None:
        _print_work_failure(run.id, run.status, "PATCHLESS_RUN_CANNOT_PROMOTE")
        return 5
    if run.status != "ready_to_promote":
        _print_work_failure(run.id, run.status, "PROMOTION_NOT_READY")
        return 5
    if run.parent_candidate_digest != patch_digest:
        _print_work_failure(run.id, run.status, "PATCH_DIGEST_MISMATCH")
        return 8
    try:
        patch, composition, snapshot = _graph_candidate(store, run)
        evaluation, policy_digest, semantic_evidence = _graph_promotion_evidence(
            store, run, patch, composition, snapshot
        )
    except (KeyError, ValueError):
        _print_work_failure(run.id, run.status, "EVIDENCE_OR_REVIEW_BLOCKED")
        return 5
    if (
        run.promotion_approval_id is None
        or run.promotion_approval_request_digest != patch.artifact_digest
    ):
        _print_work_failure(run.id, run.status, "PROMOTION_APPROVAL_REQUIRED")
        return 4
    try:
        approval = store.get("approval_v2", run.promotion_approval_id, ApprovalRecord)
    except KeyError:
        _print_work_failure(run.id, run.status, "PROMOTION_APPROVAL_REQUIRED")
        return 4
    if (
        approval.request_digest != patch.artifact_digest
        or approval.policy_digest != policy_digest
        or approval.scope != (patch.artifact_digest,)
    ):
        _print_work_failure(run.id, run.status, "STALE_PROMOTION_APPROVAL")
        return 8
    if approval.decision == "pending":
        _print_work_failure(run.id, run.status, "PROMOTION_APPROVAL_REQUIRED")
        return 4
    if approval.decision != "approved" or approval.expires_at <= now():
        _print_work_failure(run.id, run.status, "STALE_PROMOTION_APPROVAL")
        return 8
    if approval.authorization_kind == "policy_auto":
        authorities = tuple(
            item
            for item in store.list_records(
                "promotion_policy_decision_v2", PromotionPolicyDecision, run_id=run.id
            )
            if item.content_digest == approval.authorization_digest
        )
        try:
            if len(authorities) != 1 or run.repository is None:
                raise ValueError("policy approval authority is missing or ambiguous")
            acceptances = tuple(
                item
                for item in store.list_records(
                    "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run.id
                )
                if item.accepted_revision.content_digest == run.accepted_graph_revision_digest
            )
            if len(acceptances) != 1:
                raise ValueError("accepted graph authority is missing or ambiguous")
            harness = discover_project_harness(run.repository)
            operator_config = load_operator_config(run.operator_config_path)
            exact_replay = validate_exact_parent_evidence_store(
                store,
                run,
                acceptances[0].accepted_revision,
                evaluation,
                harness,
            )
            exact_semantic = tuple(
                digest
                for record in (
                    *exact_replay.semantic_requests,
                    *exact_replay.semantic_results,
                    *exact_replay.semantic_decisions,
                    *exact_replay.semantic_repair_requests,
                )
                if (digest := record.content_digest) is not None
            )
            if exact_semantic != semantic_evidence:
                raise ValueError("promotion semantic evidence is not exact")
            validate_policy_auto_authority(
                approval,
                authorities[0],
                run,
                acceptances[0].accepted_revision,
                composition,
                evaluation,
                harness,
                operator_config,
                semantic_evidence,
                harness_digest=project_harness_digest(harness),
                operator_config_digest=operator_config_digest(operator_config),
            )
        except (KeyError, OSError, TypeError, ValueError):
            _print_work_failure(run.id, run.status, "STALE_PROMOTION_APPROVAL")
            return 8
    state_root = Path(store.path).resolve().parent
    artifacts = AtomicArtifactStore(state_root / "artifacts")
    workspace = GitWorkspaceManager(Path(snapshot.isolated_worktree).resolve().parent, artifacts)
    try:
        workspace.adopt(snapshot)
        promotion = workspace.promote(snapshot, patch, approval)
    except (OSError, ValueError):
        _print_work_failure(run.id, run.status, "WORKSPACE_CONFLICT")
        return 8
    store.put("promotion_v2", promotion, run_id=run.id)
    completed = run.model_copy(update={"status": "completed", "generation": run.generation + 1})
    store.put(
        "graph_run_v2",
        completed,
        run_id=completed.id,
        revision=completed.generation + 1,
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
        artifact_store=artifacts,
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


def _resume_graph(store: SQLiteStore, run: GraphRunRecord) -> int:
    if run.status not in {"planned", "paused"} or run.repository is None:
        raise ValueError("only an authoritative planned or paused graph can start")
    return _work(
        argparse.Namespace(
            # Resume the exact explicit authority source; never discover a new default.
            operator_config=run.operator_config_path,
            repo=run.repository,
            goal=run.goal.statement,
            db=store.path,
            routing_mode=run.routing_mode.value,
            strategy=run.fixed_strategy_id,
            strategy_set=run.strategy_set,
            assessment_strategy=None,
            planner_strategy=None,
            plan_only=False,
            max_concurrency=run.max_concurrency,
            non_interactive=True,
            json=True,
            resume_graph_run=run,
        )
    )


def _next_actions(run: object) -> tuple[str, ...]:
    status = getattr(run, "status", "failed")
    run_id = getattr(run, "id", "")
    if status == "waiting_approval":
        return (f"fleet approvals list --run {run_id}", f"fleet resume {run_id}")
    if status == "ready_to_promote":
        return (f"fleet diff {run_id}", f"fleet promote {run_id} --patch-digest <digest>")
    if status in {"planned", "paused"}:
        return (f"fleet inspect {run_id}", f"fleet resume {run_id}")
    if status in {"failed", "cancelled"}:
        actions = [
            f"fleet inspect {run_id}",
            f"fleet explain {run_id}",
            f"fleet logs {run_id}",
        ]
        if (
            getattr(run, "patch_artifact_id", None) is not None
            or getattr(run, "parent_candidate_artifact_id", None) is not None
        ):
            actions.insert(1, f"fleet diff {run_id}")
        return tuple(actions)
    return (f"fleet inspect {run_id}",) if run_id else ()


def _work_goal(
    run_id: str,
    statement: str,
    harness: ProjectHarnessV2,
    *,
    task_kind: GoalTaskKind = GoalTaskKind.MUTATING,
    processes_authorized: bool | None = None,
) -> Goal:
    """Bind the original Goal to exact declared parent verification evidence."""

    processes_allowed = (
        task_kind is GoalTaskKind.MUTATING if processes_authorized is None else processes_authorized
    )
    if task_kind is GoalTaskKind.NON_MUTATING:
        return Goal(
            id=f"goal-{run_id}",
            statement=statement,
            task_kind=task_kind,
            processes_authorized=processes_allowed,
        )
    if not processes_allowed and (
        harness.verification.required or harness.verification.required_evaluators
    ):
        raise ValueError("mutating Goal requires Harness verification processes")

    evaluators = {item.id: item for item in harness.evaluators}
    criteria: list[CompletionCriterion] = []
    for evaluator_id in harness.verification.required_evaluators:
        evaluator = evaluators[evaluator_id]
        command_ref = evaluator.command_ref
        if evaluator.provider_id == "browser.playwright":
            if evaluator.browser_scenario is None:
                raise ValueError("required parent browser evaluator has no scenario")
            description = (
                "the exact composed candidate passes declared browser scenario "
                f"{canonical_digest(evaluator.browser_scenario)}"
            )
            verification_requirements: tuple[str, ...] = ()
        else:
            if command_ref is None:
                raise ValueError("required parent evaluator has no Harness command")
            description = (
                f"the exact composed candidate passes declared Harness command {command_ref}"
            )
            verification_requirements = (command_ref,)
        for criterion_id in evaluator.criterion_ids:
            criteria.append(
                CompletionCriterion(
                    id=criterion_id,
                    description=description,
                    verification_requirement_ids=verification_requirements,
                    required_artifact_ids=("workspace_patch",),
                )
            )
    return Goal(
        id=f"goal-{run_id}",
        statement=statement,
        task_kind=task_kind,
        processes_authorized=processes_allowed,
        completion_criteria=tuple(criteria),
    )


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
