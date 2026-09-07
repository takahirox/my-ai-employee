import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_employee.domain import (
    Budget,
    CompletionCriterion,
    Edge,
    ExecutionPolicy,
    ExecutionStrategy,
    Goal,
    Graph,
    Node,
    NodeKind,
    OutputContract,
    RoutingMode,
)
from ai_employee.domain.v2 import CriterionEvidence, WorkerRequest, WorkerResult
from ai_employee.routing_history import load_verified_routing_history
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    GraphRunRecord,
    NodeExecutionResult,
    NodeRouteRecord,
    TaskOrchestrator,
)

ZERO = "0" * 64
CONFIG = "1" * 64
STRATEGIES = tuple(
    ExecutionStrategy(
        id=name,
        routing_mode=RoutingMode.ADAPTIVE,
        backend="scripted",
        model=name,
        effort="bounded",
        capabilities=("process",),
    )
    for name in ("alpha", "beta")
)
CRITERION = CompletionCriterion(id="checked", description="the bounded check passes")
GOAL = Goal(id="goal", statement="run a bounded check", completion_criteria=(CRITERION,))
GRAPH = Graph(
    id="graph",
    nodes=(
        Node(
            id="node",
            kind=NodeKind.FUNCTION,
            name="bounded check",
            objective="run a bounded check",
            output_contract=OutputContract(id="output"),
            required_capabilities=("process",),
            completion_criteria=(CRITERION,),
            complexity=2,
            scale=1,
            risk=0,
        ),
    ),
    entry_node_ids=("node",),
    terminal_node_ids=("node",),
    budget=Budget(max_nodes=1, max_attempts=1, max_wall_seconds=100.0),
)
POLICY = ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=100.0)


def execute(
    store: SQLiteStore,
    repository: Path,
    run_id: str,
    *,
    fixed: str | None = None,
    succeeded: bool = True,
    seconds: float = 1.0,
    harness: str = ZERO,
    config: str = CONFIG,
    pause: bool = False,
    pause_after_result: bool = False,
    runtime_failure: str | None = None,
    strategies: tuple[ExecutionStrategy, ...] = STRATEGIES,
) -> tuple[TaskOrchestrator, GraphRunRecord]:
    store.claim_run_id(run_id, repository)
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]

    def runner(
        node: Node, request: WorkerRequest, _strategy: ExecutionStrategy
    ) -> NodeExecutionResult:
        clock[0] += timedelta(seconds=seconds)
        if pause_after_result:
            with SQLiteStore(store.path) as control:
                control.request_control(run_id, "pause")
        return NodeExecutionResult(
            failure_code=runtime_failure,
            worker_result=WorkerResult(
                id="result-" + request.run_id,
                run_id=request.run_id,
                created_at=clock[0],
                request_digest=request.content_digest or ZERO,
                status="succeeded",
                duration_seconds=0.01,
            ),
            # A worker's success with absent evidence must never train a success.
            criterion_evidence=(
                CriterionEvidence(
                    criterion_id="checked",
                    disposition="satisfied",
                    evidence_refs=(ZERO,),
                ),
            )
            if succeeded
            else (),
        )

    orchestrator = TaskOrchestrator(
        store,
        runner,
        strategies,
        routing_mode=RoutingMode.FIXED if fixed else RoutingMode.ADAPTIVE,
        fixed_strategy_id=fixed,
        repository=str(repository),
        operator_config_digest=config,
        clock=lambda: clock[0],
    )
    if pause:
        store.request_control(run_id, "pause")
    task_graph = GRAPH
    task_policy = POLICY
    if pause_after_result:
        task_graph = GRAPH.model_copy(
            update={
                "nodes": (
                    GRAPH.nodes[0],
                    GRAPH.nodes[0].model_copy(update={"id": "next", "complexity": 3}),
                ),
                "edges": (Edge(id="next-edge", source_id="node", target_id="next"),),
                "terminal_node_ids": ("next",),
                "budget": GRAPH.budget.model_copy(update={"max_nodes": 2, "max_attempts": 2}),
            }
        )
        task_policy = POLICY.model_copy(update={"max_nodes": 2, "max_attempts": 2})
    result = orchestrator.run(
        GOAL,
        task_graph,
        task_policy,
        run_id=run_id,
        harness_digest=harness,
        effective_policy_digest=ZERO,
        available_capabilities=("process",),
    )
    return orchestrator, result


def history(store: SQLiteStore, run_id: str):
    run = store.get("graph_run_v2", run_id, GraphRunRecord)
    route = store.list_records("node_route_v2", NodeRouteRecord, run_id=run_id)[0]
    return load_verified_routing_history(
        store,
        run_id=run_id,
        strategies=run.execution_strategies,
        assessment=route.assessment,
        task_kind=run.goal.task_kind,
        harness_digest=run.harness_digest,
        effective_policy_digest=run.effective_policy_digest,
        operator_config_digest=run.operator_config_digest,
    )


def train(store: SQLiteStore, repository: Path) -> None:
    for i in range(3):
        assert (
            execute(store, repository, f"alpha-{i}", fixed="alpha", succeeded=False)[1].status
            == "failed"
        )
        assert (
            execute(store, repository, f"beta-{i}", fixed="beta", seconds=2.0)[1].status
            == "completed"
        )


def test_runtime_uses_verified_history_and_replay_resume_do_not_duplicate_it(
    tmp_path: Path,
) -> None:
    db = tmp_path / "history.db"
    repo = tmp_path / "repo"
    with SQLiteStore(db) as store:
        train(store, repo)
    with SQLiteStore(db) as store:
        orchestrator, run = execute(store, repo, "adaptive")
        replay = orchestrator.replay(run.id)
        assert replay.routes[0].selected_strategy.id == "beta"
        assert replay.routes[0].performance_history_digests
        stats = {item.strategy_id: item for item in history(store, run.id).performances}
        assert (stats["alpha"].sample_count, stats["alpha"].success_count) == (3, 0)
        assert (stats["beta"].sample_count, stats["beta"].success_count) == (3, 3)
        # Runtime timestamps, not the worker's self-reported 0.01 seconds.
        assert stats["beta"].total_duration_seconds == 6.0
        expected_sources = history(store, run.id).evidence_digests
        orchestrator.replay(run.id)
        with pytest.raises(ValueError, match="planned or paused"):
            orchestrator.run(
                GOAL,
                GRAPH,
                POLICY,
                run_id=run.id,
                harness_digest=ZERO,
                effective_policy_digest=ZERO,
                available_capabilities=("process",),
                resume=True,
            )
        assert history(store, run.id).evidence_digests == expected_sources


@pytest.mark.parametrize("mismatch", ["repository", "harness", "configuration", "model", "effort"])
def test_unrelated_or_reconfigured_history_is_not_reused(tmp_path: Path, mismatch: str) -> None:
    with SQLiteStore(tmp_path / "scope.db") as store:
        repo = tmp_path / "repo"
        train(store, repo)
        strategies = STRATEGIES
        if mismatch in {"model", "effort"}:
            strategies = tuple(item.model_copy(update={mismatch: "changed"}) for item in strategies)
        orchestrator, run = execute(
            store,
            tmp_path / "other" if mismatch == "repository" else repo,
            "new",
            harness="2" * 64 if mismatch == "harness" else ZERO,
            config="2" * 64 if mismatch == "configuration" else CONFIG,
            strategies=strategies,
        )
        route = orchestrator.replay(run.id).routes[0]
        assert route.selected_strategy.id == "alpha"
        assert not route.performance_history_digests
        assert not history(store, run.id).performances


def test_cold_start_and_incomplete_or_stale_history_do_not_grant_success(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "incomplete.db") as store:
        repo = tmp_path / "repo"
        for i in range(3):
            _, prior = execute(store, repo, f"old-{i}", fixed="beta")
            # Simulate incomplete publication; an older completed revision must not win.
            store.put(
                "graph_run_v2",
                prior.model_copy(update={"status": "verifying"}),
                run_id=prior.id,
                revision=100,
            )
        orchestrator, run = execute(store, repo, "cold")
        assert orchestrator.replay(run.id).routes[0].selected_strategy.id == "alpha"
        assert not history(store, run.id).performances


def test_optional_history_failure_does_not_block_current_task(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "stale.db") as store:
        repo = tmp_path / "repo"
        train(store, repo)
        for i in range(3):
            # A well-formed route with a foreign policy cannot supply training authority.
            route = store.list_records("node_route_v2", NodeRouteRecord, run_id=f"beta-{i}")[0]
            invalid = route.model_copy(
                update={"effective_policy_digest": "9" * 64, "content_digest": None}
            )
            store.put("node_route_v2", invalid, run_id=route.run_id)
        _, run = execute(store, repo, "new")
        assert {item.strategy_id for item in history(store, run.id).performances} == {"alpha"}
        assert run.status == "completed"


def test_history_provenance_extends_routes_without_changing_legacy_digests(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "legacy.db") as store:
        _, run = execute(store, tmp_path / "repo", "cold")
        route = store.list_records("node_route_v2", NodeRouteRecord, run_id=run.id)[0]
        payload = route.model_dump(mode="json")
        payload.pop("performance_history_digests")
        assert (
            NodeRouteRecord.model_validate_json(json.dumps(payload)).content_digest
            == route.content_digest
        )
        payload["performance_history_digests"] = ["9" * 64]
        with pytest.raises(ValueError):
            NodeRouteRecord.model_validate_json(json.dumps(payload))


def test_paused_node_only_contributes_once_after_verified_resume(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "resume.db") as store:
        repo = tmp_path / "repo"
        orchestrator, paused = execute(store, repo, "paused", fixed="beta", pause=True)
        assert paused.status == "paused"
        _, observer = execute(store, repo, "observer", fixed="alpha")
        assert not history(store, observer.id).performances
        store.clear_control(paused.id)
        finished = orchestrator.run(
            GOAL,
            GRAPH,
            POLICY,
            run_id=paused.id,
            harness_digest=ZERO,
            effective_policy_digest=ZERO,
            available_capabilities=("process",),
            resume=True,
        )
        assert finished.status == "completed"
        observed = history(store, observer.id)
        assert [
            (item.strategy_id, item.sample_count, item.success_count)
            for item in observed.performances
        ] == [("beta", 1, 1)]
        orchestrator.replay(finished.id)
        assert history(store, observer.id).evidence_digests == observed.evidence_digests


@pytest.mark.parametrize("mismatch", ["task_kind", "policy", "risk", "complexity", "context"])
def test_history_respects_task_class_and_policy(tmp_path: Path, mismatch: str) -> None:
    from ai_employee.domain import GoalTaskKind

    with SQLiteStore(tmp_path / "task-class.db") as store:
        repo = tmp_path / "repo"
        train(store, repo)
        _, run = execute(store, repo, "query")
        route = store.list_records("node_route_v2", NodeRouteRecord, run_id=run.id)[0]
        assessment = route.assessment
        if mismatch in {"risk", "complexity"}:
            assessment = assessment.model_copy(update={mismatch: 9})
        elif mismatch == "context":
            assessment = assessment.model_copy(update={"context_character_count": 9000})
        queried = load_verified_routing_history(
            store,
            run_id=run.id,
            strategies=STRATEGIES,
            assessment=assessment,
            task_kind=GoalTaskKind.NON_MUTATING if mismatch == "task_kind" else GOAL.task_kind,
            harness_digest=ZERO,
            effective_policy_digest="8" * 64 if mismatch == "policy" else ZERO,
            operator_config_digest=CONFIG,
        )
        assert not queried.performances


def test_completed_node_resume_preserves_one_original_sample_and_duration(tmp_path):
    from ai_employee.task_orchestration import NodeExecutionRecord

    with SQLiteStore(tmp_path / "retained.db") as store:
        repo = tmp_path / "repo"
        scheduler, paused = execute(
            store, repo, "retained", fixed="beta", seconds=2.0, pause_after_result=True
        )
        assert paused.status == "paused"
        scheduler.clock = lambda: datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=50)
        accepted = scheduler.replay(paused.id).acceptance.accepted_revision.graph
        resumed = scheduler.run(
            GOAL,
            accepted,
            paused.execution_policy,
            run_id=paused.id,
            harness_digest=ZERO,
            effective_policy_digest=ZERO,
            available_capabilities=("process",),
            resume=True,
        )
        assert resumed.status == "completed"
        _, observer = execute(store, repo, "observer")
        observed = history(store, observer.id)
        assert [
            (item.sample_count, item.success_count, item.total_duration_seconds)
            for item in observed.performances
        ] == [(1, 1, 2.0)]
        assert history(store, observer.id).evidence_digests == observed.evidence_digests
        records = store.list_records("node_execution_v2", NodeExecutionRecord, run_id=paused.id)
        original = next(
            item for item in records if item.status == "passed" and item.generation == 0
        )
        assert original.content_digest in observed.evidence_digests
        # A retained copy alone is insufficient: the original execution is required.
        with store.transaction() as connection:
            connection.execute(
                "DELETE FROM records WHERE kind='node_execution_v2' AND record_id=?", (original.id,)
            )
        assert not history(store, observer.id).performances


@pytest.mark.parametrize(
    "code",
    [
        "NODE_ARTIFACT_BUDGET_EXCEEDED",
        "NODE_PROCESS_BUDGET_EXCEEDED",
        "TIMEOUT",
        "CANCELLED",
        "NETWORK_BLOCKED",
    ],
)
def test_runtime_failure_with_node_evaluator_fail_does_not_train_model_quality(tmp_path, code):
    with SQLiteStore(tmp_path / "runtime.db") as store:
        repo = tmp_path / "repo"
        _, failed = execute(store, repo, "capacity", fixed="alpha", runtime_failure=code)
        assert failed.status == "failed"
        _, observer = execute(store, repo, "observer")
        assert not history(store, observer.id).performances
