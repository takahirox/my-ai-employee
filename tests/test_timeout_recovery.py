from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

import ai_employee.task_orchestration as scheduler
from ai_employee.domain import ExecutionPolicy, ExecutionStrategy, Goal, RoutingMode
from ai_employee.domain.v2 import CriterionEvidence, StableFailure, StableFailureCode, WorkerResult
from ai_employee.inspector import inspect_graph_run
from ai_employee.serialization import versioned_digest
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import NodeExecutionResult, TaskOrchestrator, one_node_graph
from ai_employee.worker_supervision import (
    TimeoutRecoveryContext,
    TimeoutRecoveryRecord,
    WorkerSupervisionPolicy,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _exercise(
    tmp_path,
    monkeypatch,
    *,
    source="adapter",
    cleanup="sigterm_confirmed",
    elapsed=30.0,
    retries=1,
    always_timeout=False,
    cancel=False,
    wrong_binding=False,
    failure_code=StableFailureCode.TIMEOUT,
):
    goal = Goal(id="goal", statement="complete bounded work")
    base = one_node_graph(
        goal,
        graph_id="graph",
        node_id="node",
        max_wall_seconds=30.0,
        required_capabilities=("process",),
    )
    graph = base.model_copy(
        update={
            "nodes": (base.nodes[0].model_copy(update={"retry_limit": retries}),),
            "budget": base.budget.model_copy(
                update={"max_attempts": 2, "max_retries": retries, "max_wall_seconds": 90.0}
            ),
        }
    )
    strategy = ExecutionStrategy(
        id="strategy",
        routing_mode=RoutingMode.FIXED,
        backend="scripted",
        model="fixture",
        capabilities=("process",),
    )
    attempts = []
    time = {"elapsed": 0.0, "monotonic": 0.0}

    def runner(node, request, selected):
        attempts.append((node.attempt, selected.id, selected.model, selected.backend))
        failed = always_timeout or node.attempt == 0
        time["elapsed"] = elapsed if failed else elapsed + 1.0
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id=f"result-{node.attempt}",
                run_id=request.run_id,
                created_at=NOW,
                request_digest="f" * 64 if wrong_binding else request.content_digest,
                status="failed" if failed else "succeeded",
                failure=StableFailure(code=failure_code, message="adapter deadline")
                if failed
                else None,
                resource_usage={"process_group_cleanup": cleanup},
                stdout_artifact_digest="c" * 64,
                stderr_artifact_digest="d" * 64,
                duration_seconds=30.0 if failed else 1.0,
            ),
            criterion_evidence=()
            if failed
            else (
                CriterionEvidence(
                    criterion_id=node.completion_criteria[0].id,
                    disposition="satisfied",
                    evidence_refs=("a" * 64,),
                ),
            ),
        )

    real_wait = scheduler.wait
    hidden_completions = set()
    with SQLiteStore(tmp_path / "recovery.db") as store:

        def wait(futures, *, timeout, return_when):
            done, pending = real_wait(futures, timeout=1.0, return_when=return_when)
            assert done
            if cancel:
                store.request_control("run", "cancel")
            if source == "scheduler" and not done <= hidden_completions:
                hidden_completions.update(done)
                time["monotonic"] += 30.0
                return set(), set(futures)
            return done, pending

        monkeypatch.setattr(scheduler, "now", lambda: NOW + timedelta(seconds=time["elapsed"]))
        monkeypatch.setattr(scheduler, "wait", wait)
        monkeypatch.setattr(scheduler, "monotonic", lambda: time["monotonic"])
        orchestrator = TaskOrchestrator(
            store,
            runner,
            (strategy,),
            routing_mode=RoutingMode.FIXED,
            fixed_strategy_id=strategy.id,
            clock=lambda: NOW + timedelta(seconds=time["elapsed"]),
            # Advance fake time without a real 30-second polling loop; isolate deadline
            # recovery from owner-heartbeat behavior covered by separate lease tests.
            lease_duration_seconds=1000.0,
            worker_supervision_policy=WorkerSupervisionPolicy(),
        )
        run = orchestrator.run(
            goal,
            graph,
            ExecutionPolicy(max_nodes=1, max_attempts=2, max_wall_seconds=90.0),
            harness_digest="a" * 64,
            effective_policy_digest="b" * 64,
            run_id="run",
            available_capabilities=("process",),
        )
    # Reopen the database: this must not depend on in-memory recovery bookkeeping.
    with SQLiteStore(tmp_path / "recovery.db") as store:
        replay = TaskOrchestrator(store, runner, (strategy,)).replay(run.id)
        recoveries = store.list_records("timeout_recovery_v2", TimeoutRecoveryRecord, run_id=run.id)
        results = store.list_records("worker_result_v2", WorkerResult, run_id=run.id)
        view = inspect_graph_run(store, run.id)
    recoveries = tuple(sorted(recoveries, key=lambda item: item.source_attempt))
    return run, replay, recoveries, attempts, results, view


def test_adapter_timeout_uses_authorized_same_strategy_retry(tmp_path, monkeypatch):
    run, replay, recoveries, attempts, results, view = _exercise(tmp_path, monkeypatch)
    assert run.status == "completed"
    assert replay.nodes[0].status == "passed"
    assert attempts == [
        (0, "strategy", "fixture", "scripted"),
        (1, "strategy", "fixture", "scripted"),
    ]
    assert len(recoveries) == 1
    recovery = recoveries[0]
    assert recovery.action == "same_strategy_retry"
    assert recovery.context is not None
    assert recovery.context.source == "adapter"
    assert recovery.context.remaining_run_seconds == 60.0
    timed_out = next(result for result in results if result.id == "result-0")
    assert recovery.context.worker_result_digest == timed_out.content_digest
    assert timed_out.stdout_artifact_digest == "c" * 64
    assert timed_out.stderr_artifact_digest == "d" * 64
    assert view["timeout_recoveries"][0]["context"]["cleanup_confirmed"] is True


@pytest.mark.parametrize(
    "source,cleanup",
    [
        ("adapter", None),
        ("adapter", "failed"),
        ("scheduler", "failed"),
        ("adapter", "unknown"),
        ("scheduler", "unknown"),
        ("adapter", {"unexpected": "shape"}),
        ("scheduler", {"unexpected": "shape"}),
    ],
)
def test_unknown_or_failed_cleanup_never_retries(tmp_path, monkeypatch, source, cleanup):
    run, _, recovery, attempts, _, _ = _exercise(
        tmp_path, monkeypatch, source=source, cleanup=cleanup
    )
    assert run.status == "failed"
    assert len(attempts) == 1
    assert recovery[0].action == "denied"
    assert recovery[0].context.cleanup_confirmed is False


@pytest.mark.parametrize("source", ["adapter", "scheduler"])
@pytest.mark.parametrize("elapsed", [89.5, 90.0, 100.0])
def test_remaining_real_time_prevents_doomed_retry(tmp_path, monkeypatch, source, elapsed):
    run, _, recovery, attempts, results, _ = _exercise(
        tmp_path, monkeypatch, source=source, elapsed=elapsed
    )
    assert run.status == "failed"
    assert len(attempts) == 1
    if source == "scheduler" and elapsed >= 90.0:
        assert run.failure_code == "RUN_WALL_BUDGET_EXCEEDED"
        assert not recovery  # The overall deadline precedes a per-attempt recovery decision.
        assert len(results) == 1 and results[0].failure.code is StableFailureCode.TIMEOUT
        return
    assert recovery[0].action == "denied"
    assert recovery[0].context.remaining_run_seconds == max(0.0, 90.0 - elapsed)


@pytest.mark.parametrize("source", ["adapter", "scheduler"])
def test_timeout_does_not_create_retry_authority(tmp_path, monkeypatch, source):
    run, _, recovery, attempts, _, _ = _exercise(tmp_path, monkeypatch, source=source, retries=0)
    assert run.status == "failed"
    assert len(attempts) == 1
    assert recovery[0].action == "denied"


@pytest.mark.parametrize("source", ["adapter", "scheduler"])
def test_repeated_timeouts_stop_at_accepted_attempt_limit(tmp_path, monkeypatch, source):
    run, _, recovery, attempts, _, _ = _exercise(
        tmp_path, monkeypatch, source=source, always_timeout=True
    )
    assert run.status == "failed"
    assert len(attempts) == 2
    assert [item.action for item in recovery] == ["same_strategy_retry", "denied"]
    assert [item.source_attempt for item in recovery] == [0, 1]


@pytest.mark.parametrize("source", ["adapter", "scheduler"])
def test_cancel_takes_precedence_over_timeout_retry(tmp_path, monkeypatch, source):
    run, _, recovery, attempts, _, _ = _exercise(tmp_path, monkeypatch, source=source, cancel=True)
    assert run.status == "cancelled"
    assert len(attempts) == 1
    assert recovery == ()


@pytest.mark.parametrize("source", ["adapter", "scheduler"])
def test_wrong_result_binding_cannot_authorize_recovery(tmp_path, monkeypatch, source):
    run, replay, recovery, attempts, results, _ = _exercise(
        tmp_path, monkeypatch, source=source, wrong_binding=True
    )
    assert run.status == "failed"
    assert replay.nodes[0].failure_code == "WORKER_BOUNDARY_ERROR"
    assert len(attempts) == 1
    assert recovery == ()
    assert results == ()


@pytest.mark.parametrize("source", ["adapter", "scheduler"])
@pytest.mark.parametrize(
    "code",
    [
        StableFailureCode.BUDGET_EXCEEDED,
        StableFailureCode.PROCESS_FAILED,
    ],
)
def test_other_failures_do_not_become_timeout_retries(tmp_path, monkeypatch, code, source):
    run, _, recovery, attempts, _, _ = _exercise(
        tmp_path, monkeypatch, failure_code=code, source=source
    )
    assert run.status == "failed"
    assert len(attempts) == 1
    assert recovery == ()


def test_recovery_context_is_digest_bound_and_legacy_records_load(tmp_path, monkeypatch):
    _, _, recoveries, _, _, _ = _exercise(tmp_path, monkeypatch)
    recovery = recoveries[0]
    payload = recovery.model_dump(mode="json")
    payload["context"]["remaining_run_seconds"] += 1
    with pytest.raises(ValueError, match="content_digest"):
        TimeoutRecoveryRecord.model_validate_json(json.dumps(payload))
    legacy = recovery.model_dump(exclude={"content_digest", "context"})
    original_content = {
        key: value for key, value in legacy.items() if key not in {"id", "run_id", "created_at"}
    }
    legacy["content_digest"] = versioned_digest(original_content)
    loaded = TimeoutRecoveryRecord.model_validate(legacy)
    assert loaded.context is None
    assert loaded.content_digest == legacy["content_digest"]


@pytest.mark.parametrize("cleanup,remaining", [(False, 60.0), (True, 0.0), (True, 0.5)])
def test_recovery_record_cannot_claim_unsafe_retry(tmp_path, monkeypatch, cleanup, remaining):
    _, _, recoveries, _, _, _ = _exercise(tmp_path, monkeypatch)
    payload = recoveries[0].model_dump(exclude={"content_digest"})
    payload["context"]["cleanup_confirmed"] = cleanup
    payload["context"]["remaining_run_seconds"] = remaining
    with pytest.raises(ValueError, match=r"timeout (recovery|retry)"):
        TimeoutRecoveryRecord.model_validate(payload)


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1.0])
def test_recovery_context_requires_finite_remaining_time(duration):
    with pytest.raises(ValueError):
        TimeoutRecoveryContext(
            source="adapter",
            worker_request_digest="a" * 64,
            worker_result_digest="b" * 64,
            cleanup_confirmed=True,
            remaining_run_seconds=duration,
            minimum_retry_seconds=1.0,
        )
