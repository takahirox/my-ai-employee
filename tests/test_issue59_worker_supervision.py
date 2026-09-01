from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import pytest

from ai_employee.domain import (
    ExecutionPolicy,
    ExecutionStrategy,
    Goal,
    Node,
    RoutingMode,
    SemanticAmbiguity,
    SemanticReasoningClass,
    SemanticScope,
    SemanticTaskProfile,
    SemanticTaskType,
)
from ai_employee.domain.v2 import WorkerRequest
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    NodeExecutionResult,
    NodeRunner,
    TaskOrchestrator,
    one_node_graph,
)
from ai_employee.worker_supervision import (
    TimeoutRecoveryRecord,
    WorkerAttemptObservation,
    WorkerAttemptSupervisor,
    WorkerBudgetPreflightRecord,
    WorkerSupervisionPolicy,
    WorkerTimeoutProfileRecord,
    WorkerTimeoutRule,
    inadequate_authorities,
    select_node_timeout,
    timeout_recovery_action,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
DIGEST = "a" * 64


def _profile() -> SemanticTaskProfile:
    return SemanticTaskProfile(
        task_type=SemanticTaskType.IMPLEMENTATION,
        reasoning_class=SemanticReasoningClass.DEEP,
        scope=SemanticScope.MULTI_COMPONENT,
        ambiguity=SemanticAmbiguity.LOW,
        reasons=("bounded but deep implementation",),
    )


def _timeout(
    *, accepted: float = 1800.0, rule: WorkerTimeoutRule | None = None
) -> WorkerTimeoutProfileRecord:
    selected_rule = rule or WorkerSupervisionPolicy().select(
        _profile().scope, _profile().reasoning_class, 7
    )
    return select_node_timeout(
        id="timeout-profile",
        run_id="run",
        created_at=NOW,
        graph_run_id="run",
        node_id="node",
        child_run_id="child",
        accepted_graph_revision_digest=DIGEST,
        generation=0,
        attempt=0,
        operator_config_digest="e" * 64,
        rule=selected_rule,
        profile=_profile(),
        scale=7,
        accepted_node_timeout_seconds=accepted,
        adapter_timeout_seconds=1800.0,
        policy_timeout_seconds=2000.0,
        remaining_run_timeout_seconds=1900.0,
    )


def test_versioned_profile_selects_recommended_and_exact_authority_minimum() -> None:
    timeout = _timeout()

    assert timeout.rule_version == "1"
    assert timeout.rule_id == "deep"
    assert timeout.recommended_timeout_seconds == 1800.0
    assert timeout.profile_minimum_seconds == 1200.0
    assert timeout.effective_timeout_seconds == 1800.0
    assert inadequate_authorities(timeout) == ()


def test_accepted_node_below_profile_minimum_is_denied_without_extension() -> None:
    timeout = _timeout(accepted=1199.999)

    assert inadequate_authorities(timeout) == ("accepted_node",)
    assert timeout.accepted_node_timeout_seconds == 1199.999
    assert timeout.recommended_timeout_seconds == 1800.0
    assert timeout.effective_timeout_seconds == 1199.999


def test_inadequate_budget_preflight_never_launches_worker(tmp_path: Path) -> None:
    goal = Goal(id="goal", statement="deep implementation")
    base = one_node_graph(
        goal,
        graph_id="graph",
        node_id="node",
        required_capabilities=("process",),
        max_wall_seconds=800.0,
    )
    deep_node = base.nodes[0].model_copy(
        update={"semantic_profile": _profile(), "complexity": 7, "scale": 7}
    )
    graph = base.model_copy(
        update={
            "nodes": (deep_node,),
            "budget": base.budget.model_copy(update={"max_wall_seconds": 1800.0}),
        }
    )
    strategy = ExecutionStrategy(
        id="strategy",
        routing_mode=RoutingMode.ADAPTIVE,
        backend="scripted",
        model="fixture",
        effort="deep",
        capabilities=("process",),
    )
    launches = 0

    def runner(
        _node: Node, _request: WorkerRequest, _strategy: ExecutionStrategy
    ) -> NodeExecutionResult:
        nonlocal launches
        launches += 1
        raise AssertionError("preflight-denied worker was launched")

    with SQLiteStore(tmp_path / "preflight.db") as store:
        result = TaskOrchestrator(
            store,
            cast(NodeRunner, runner),
            (strategy,),
            worker_supervision_policy=WorkerSupervisionPolicy(),
            adapter_timeout_seconds=1800.0,
        ).run(
            goal,
            graph,
            ExecutionPolicy(max_nodes=1, max_attempts=1, max_wall_seconds=1800.0),
            harness_digest=DIGEST,
            effective_policy_digest="b" * 64,
            run_id="run-preflight",
            available_capabilities=("process",),
        )
        preflights = store.list_records(
            "worker_budget_preflight_v2", WorkerBudgetPreflightRecord, run_id=result.id
        )
        requests = store.list_records("worker_request_v2", WorkerRequest, run_id=result.id)

    assert result.status == "failed"
    assert launches == 0
    assert len(preflights) == 1
    assert preflights[0].denied_authorities == ("accepted_node",)
    assert requests == ()


def test_silent_attempt_reaches_exact_800_second_hard_timeout() -> None:
    timeout = _timeout(
        accepted=800.0,
        rule=WorkerTimeoutRule(
            id="legacy-silent-800",
            scope=SemanticScope.MULTI_COMPONENT,
            reasoning_class=SemanticReasoningClass.DEEP,
            min_scale=7,
            max_scale=7,
            recommended_timeout_seconds=800.0,
            minimum_timeout_seconds=1.0,
        ),
    )
    supervisor = WorkerAttemptSupervisor(
        timeout,
        heartbeat_interval_seconds=100.0,
        no_progress_threshold_seconds=200.0,
    )
    silent = WorkerAttemptObservation(process_status="running")

    first = supervisor.sample(silent, elapsed_seconds=0.0, observed_at=NOW)
    diagnostic = supervisor.sample(
        silent, elapsed_seconds=300.0, observed_at=NOW + timedelta(seconds=300)
    )
    hard = supervisor.sample(
        WorkerAttemptObservation(process_status="cancelled"),
        elapsed_seconds=800.0,
        observed_at=NOW + timedelta(seconds=800),
        force=True,
    )

    assert first is not None and first.hard_timeout_reached is False
    assert diagnostic is not None and diagnostic.silence_diagnostic is True
    assert diagnostic.model_stuck is False
    assert diagnostic.early_cancel_authorized is False
    assert hard is not None and hard.hard_timeout_reached is True
    assert hard.remaining_seconds == 0.0


def test_870_5536_second_success_is_not_cut_off_under_1800_seconds() -> None:
    timeout = _timeout()
    supervisor = WorkerAttemptSupervisor(timeout, heartbeat_interval_seconds=30.0)
    succeeded = supervisor.sample(
        WorkerAttemptObservation(
            process_status="succeeded",
            stdout_bytes=143,
            last_artifact_digest="b" * 64,
            last_diff_digest="c" * 64,
        ),
        elapsed_seconds=870.5536,
        observed_at=NOW + timedelta(seconds=870.5536),
        force=True,
    )

    assert succeeded is not None
    assert succeeded.hard_timeout_reached is False
    assert succeeded.remaining_seconds == pytest.approx(929.4464)
    assert succeeded.process_status == "succeeded"


def test_periodic_heartbeats_record_progress_and_diagnostic_only_silence() -> None:
    supervisor = WorkerAttemptSupervisor(
        _timeout(),
        heartbeat_interval_seconds=30.0,
        no_progress_threshold_seconds=60.0,
    )
    initial = WorkerAttemptObservation(process_status="running")
    assert supervisor.sample(initial, elapsed_seconds=0.0, observed_at=NOW) is not None
    assert supervisor.sample(
        initial, elapsed_seconds=29.0, observed_at=NOW + timedelta(seconds=29)
    ) is None
    silent = supervisor.sample(
        initial, elapsed_seconds=60.0, observed_at=NOW + timedelta(seconds=60)
    )
    progressed = supervisor.sample(
        WorkerAttemptObservation(
            process_status="running",
            stdout_bytes=20,
            last_mediated_action_digest="d" * 64,
        ),
        elapsed_seconds=90.0,
        observed_at=NOW + timedelta(seconds=90),
    )

    assert silent is not None and silent.no_observable_progress is True
    assert silent.early_cancel_authorized is False
    assert progressed is not None and progressed.observable_progress is True
    assert progressed.no_observable_progress is False
    assert progressed.stdout_bytes == 20
    assert progressed.last_mediated_action_digest == "d" * 64


def test_heartbeat_history_is_bounded_but_forced_terminal_record_is_kept() -> None:
    supervisor = WorkerAttemptSupervisor(
        _timeout(),
        heartbeat_interval_seconds=1.0,
        no_progress_threshold_seconds=2.0,
        max_heartbeat_records=2,
    )
    observation = WorkerAttemptObservation(process_status="running")

    assert supervisor.sample(observation, elapsed_seconds=0.0, observed_at=NOW) is not None
    assert supervisor.sample(
        observation, elapsed_seconds=1.0, observed_at=NOW + timedelta(seconds=1)
    ) is not None
    assert supervisor.sample(
        observation, elapsed_seconds=2.0, observed_at=NOW + timedelta(seconds=2)
    ) is None
    terminal = supervisor.sample(
        WorkerAttemptObservation(process_status="succeeded"),
        elapsed_seconds=3.0,
        observed_at=NOW + timedelta(seconds=3),
        force=True,
    )

    assert terminal is not None
    assert terminal.sequence == 2


@pytest.mark.parametrize("routing_mode", ["fixed", "adaptive"])
def test_timeout_recovery_allows_only_exact_same_strategy(
    routing_mode: Literal["fixed", "adaptive"],
) -> None:
    action = timeout_recovery_action(
        retry_within_policy=True,
        retry_within_counters=True,
        retry_within_resource_budgets=True,
        replan_authorized=True,
    )
    recovery = TimeoutRecoveryRecord(
        id=f"recovery-{routing_mode}",
        run_id="run",
        created_at=NOW,
        graph_run_id="run",
        node_id="node",
        child_run_id="child",
        accepted_graph_revision_digest=DIGEST,
        timeout_profile_digest=DIGEST,
        source_generation=0,
        source_attempt=0,
        action=action,
        routing_mode=routing_mode,
        source_strategy_id="strategy",
        source_model="model",
        source_backend="backend",
        retry_strategy_id="strategy",
        retry_model="model",
        retry_backend="backend",
        retry_within_policy=True,
        retry_within_counters=True,
        retry_within_resource_budgets=True,
        normal_acceptance_required=False,
    )

    assert recovery.action == "same_strategy_retry"
    assert recovery.alternate_fallback_authorized is False
    with pytest.raises(ValueError, match="preserve strategy, model, and backend"):
        TimeoutRecoveryRecord.model_validate(
            {
                **recovery.model_dump(exclude={"content_digest"}),
                "retry_backend": "fallback",
            },
            strict=True,
        )


def test_budget_or_policy_denial_requires_normally_accepted_replan() -> None:
    assert timeout_recovery_action(
        retry_within_policy=False,
        retry_within_counters=True,
        retry_within_resource_budgets=True,
        replan_authorized=True,
    ) == "replan_required"
    recovery = TimeoutRecoveryRecord(
        id="recovery-replan",
        run_id="run",
        created_at=NOW,
        graph_run_id="run",
        node_id="node",
        child_run_id="child",
        accepted_graph_revision_digest=DIGEST,
        timeout_profile_digest=DIGEST,
        source_generation=0,
        source_attempt=0,
        action="replan_required",
        routing_mode="adaptive",
        source_strategy_id="strategy",
        source_model="model",
        source_backend="backend",
        retry_within_policy=False,
        retry_within_counters=True,
        retry_within_resource_budgets=True,
        normal_acceptance_required=True,
    )

    assert recovery.retry_strategy_id is None
    assert recovery.alternate_fallback_authorized is False
