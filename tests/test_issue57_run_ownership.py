from __future__ import annotations

import os
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_employee import cli
from ai_employee.domain import ExecutionPolicy, ExecutionStrategy, Goal, RoutingMode
from ai_employee.domain.v2 import WorkerRequest, WorkerResult
from ai_employee.inspector import inspect_any_run, inspect_fleet_runs
from ai_employee.run_ownership import (
    OwnerFenceViolationRecord,
    RunExecutionOwnerRecord,
    RunLeaseHeartbeatRecord,
    RunOrphanRecoveryRecord,
    RunOwnerConflictRecord,
)
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    GraphRunRecord,
    NodeExecutionRecord,
    NodeExecutionResult,
    TaskOrchestrator,
    one_node_graph,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
ZERO = "0" * 64


def _strategy() -> ExecutionStrategy:
    return ExecutionStrategy(
        id="owner-fixture",
        routing_mode=RoutingMode.ADAPTIVE,
        backend="scripted",
        model="fixture",
    )


def _run(run_id: str = "lease-run", *, status: str = "running") -> GraphRunRecord:
    return GraphRunRecord(
        id=run_id,
        goal_id=f"goal-{run_id}",
        goal=Goal(id=f"goal-{run_id}", statement="prove the parent is alive"),
        execution_policy=ExecutionPolicy(max_nodes=1, max_attempts=1),
        accepted_graph_revision_digest=ZERO,
        harness_digest="1" * 64,
        effective_policy_digest="2" * 64,
        available_capabilities=(),
        execution_strategies=(_strategy(),),
        routing_mode=RoutingMode.ADAPTIVE,
        allowed_strategy_ids=("owner-fixture",),
        allowed_backends=("scripted",),
        local_backend_allowed=False,
        status=status,  # type: ignore[arg-type]
        max_concurrency=1,
        max_claims=1,
    )


def _owner(
    run: GraphRunRecord,
    *,
    owner_id: str = "owner-one",
    attempt: int | None = None,
    acquired_at: datetime = NOW,
) -> RunExecutionOwnerRecord:
    return RunExecutionOwnerRecord(
        id=f"record-{owner_id}-{attempt if attempt is not None else run.execution_attempt}",
        run_id=run.id,
        created_at=acquired_at,
        graph_run_id=run.id,
        accepted_graph_revision_digest=run.accepted_graph_revision_digest,
        generation=run.generation,
        execution_attempt=run.execution_attempt if attempt is None else attempt,
        owner_instance_id=owner_id,
        acquired_at=acquired_at,
        last_heartbeat_at=acquired_at,
        expires_at=acquired_at + timedelta(seconds=10),
        lease_duration_seconds=10,
    )


def test_active_requires_one_current_nonexpired_exact_owner(tmp_path: Path) -> None:
    database = tmp_path / "active.db"
    with SQLiteStore(database) as store:
        run = _run()
        store.put("graph_run_v2", run, run_id=run.id)
        assert inspect_fleet_runs(store, clock=lambda: NOW)["active"] == []

        owner = _owner(run)
        assert store.acquire_run_owner(owner) is None
        overview = inspect_fleet_runs(store, clock=lambda: NOW + timedelta(seconds=5))
        assert [item["run_id"] for item in overview["active"]] == [run.id]
        assert overview["active"][0]["owner_instance_id"] == owner.owner_instance_id

        owner_records_before = store.list_records(
            "run_execution_owner_v2", RunExecutionOwnerRecord, run_id=run.id
        )
        overview = inspect_fleet_runs(store, clock=lambda: NOW + timedelta(seconds=10))
        assert overview["active"] == []
        assert overview["history"][0]["status"] == "orphaned"
        assert overview["history"][0]["diagnostic_code"] == "RUN_LEASE_EXPIRED"
        assert (
            store.list_records("run_execution_owner_v2", RunExecutionOwnerRecord, run_id=run.id)
            == owner_records_before
        )
        incidents = {
            item["code"]
            for item in inspect_any_run(store, run.id, clock=lambda: NOW + timedelta(seconds=10))[
                "doctor"
            ]["incidents"]
        }
        assert {"RUN_LEASE_EXPIRED", "PARENT_TERMINALIZATION_MISSING"} <= incidents


def test_owner_race_and_stale_heartbeat_fail_closed(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "race.db") as store:
        run = _run("race-run")
        store.put("graph_run_v2", run, run_id=run.id)
        first = _owner(run)
        second = _owner(run, owner_id="owner-two")
        assert store.acquire_run_owner(first) is None
        conflict = store.acquire_run_owner(second)
        assert conflict is not None
        assert conflict["owner_record_id"] == first.id

        stale_heartbeat = RunLeaseHeartbeatRecord(
            id="stale-heartbeat",
            run_id=run.id,
            created_at=NOW + timedelta(seconds=1),
            graph_run_id=run.id,
            accepted_graph_revision_digest=run.accepted_graph_revision_digest,
            generation=run.generation,
            execution_attempt=run.execution_attempt,
            owner_instance_id=second.owner_instance_id,
            owner_record_id=second.id,
            owner_record_digest=second.content_digest or ZERO,
            previous_heartbeat_digest=second.content_digest or ZERO,
            heartbeat_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=11),
        )
        assert store.heartbeat_run_owner(stale_heartbeat) is False
        assert store.current_run_owner(run.id)["owner_record_id"] == first.id  # type: ignore[index]
        store.put(
            "run_owner_conflict_v2",
            RunOwnerConflictRecord(
                id="race-conflict",
                run_id=run.id,
                created_at=NOW,
                graph_run_id=run.id,
                accepted_graph_revision_digest=run.accepted_graph_revision_digest,
                generation=run.generation,
                execution_attempt=run.execution_attempt,
                rejected_owner_instance_id=second.owner_instance_id,
                current_owner_record_id=first.id,
                current_owner_record_digest=first.content_digest or ZERO,
                current_owner_instance_id=first.owner_instance_id,
                current_generation=first.generation,
                current_execution_attempt=first.execution_attempt,
                last_heartbeat_at=first.last_heartbeat_at,
                expires_at=first.expires_at,
            ),
            run_id=run.id,
        )
        store.put(
            "owner_fence_violation_v2",
            OwnerFenceViolationRecord(
                id="stale-owner-fence",
                run_id=run.id,
                created_at=NOW + timedelta(seconds=1),
                graph_run_id=run.id,
                accepted_graph_revision_digest=run.accepted_graph_revision_digest,
                generation=run.generation,
                execution_attempt=run.execution_attempt,
                owner_instance_id=second.owner_instance_id,
                owner_record_id=second.id,
                owner_record_digest=second.content_digest or ZERO,
                operation="heartbeat",
                observed_at=NOW + timedelta(seconds=1),
                reason="stale",
            ),
            run_id=run.id,
        )
        incidents = {
            item["code"]
            for item in inspect_any_run(store, run.id, clock=lambda: NOW + timedelta(seconds=1))[
                "doctor"
            ]["incidents"]
        }
        assert {"RUN_OWNER_CONFLICT", "OWNER_FENCE_VIOLATION"} <= incidents


def test_expired_orphan_recovery_is_exact_idempotent_and_history_preserving(
    tmp_path: Path,
) -> None:
    with SQLiteStore(tmp_path / "recovery.db") as store:
        run = _run("recovery-run")
        store.put("graph_run_v2", run, run_id=run.id)
        owner = _owner(run)
        assert store.acquire_run_owner(owner) is None
        recovered_at = owner.expires_at + timedelta(seconds=1)
        recovery = RunOrphanRecoveryRecord(
            id="run-recovery-fixture",
            run_id=run.id,
            created_at=recovered_at,
            graph_run_id=run.id,
            accepted_graph_revision_digest=run.accepted_graph_revision_digest,
            generation=run.generation,
            execution_attempt=run.execution_attempt,
            expired_owner_record_id=owner.id,
            expired_owner_record_digest=owner.content_digest or ZERO,
            last_heartbeat_at=owner.last_heartbeat_at,
            expired_at=owner.expires_at,
            recovered_at=recovered_at,
        )
        interrupted = run.model_copy(
            update={"status": "interrupted", "failure_code": "RUN_ORPHAN_RECOVERED"}
        )
        assert (
            store.recover_expired_run_owner(interrupted, recovery, observed_at=recovered_at)
            == "recovered"
        )
        assert (
            store.recover_expired_run_owner(interrupted, recovery, observed_at=recovered_at)
            == "already_recovered"
        )
        revisions = store.list_records("graph_run_v2", GraphRunRecord, run_id=run.id)
        assert [item.status for item in revisions] == ["running", "interrupted"]
        assert (
            store.get("run_execution_owner_v2", owner.id, RunExecutionOwnerRecord).content_digest
            == owner.content_digest
        )

        newer = _owner(
            interrupted.model_copy(update={"generation": 1, "execution_attempt": 1}),
            owner_id="new-owner",
            attempt=1,
            acquired_at=recovered_at + timedelta(seconds=1),
        )
        assert store.acquire_run_owner(newer) is None
        assert (
            store.recover_expired_run_owner(
                interrupted, recovery, observed_at=recovered_at + timedelta(seconds=2)
            )
            == "stale"
        )
        assert store.current_run_owner(run.id)["owner_record_id"] == newer.id  # type: ignore[index]


def test_terminal_child_nonterminal_parent_is_not_active(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "child-parent.db") as store:
        run = _run("parent-run")
        store.put("graph_run_v2", run, run_id=run.id)
        assert store.acquire_run_owner(_owner(run)) is None
        child = NodeExecutionRecord(
            id="terminal-child-node",
            run_id=run.id,
            created_at=NOW,
            node_id="node-one",
            accepted_graph_revision_digest=run.accepted_graph_revision_digest,
            generation=run.generation,
            attempt=0,
            sequence=1,
            transitioned_at=NOW,
            status="passed",
            work_run_id="child-work-run",
        )
        store.put("node_execution_v2", child, run_id=run.id)
        overview = inspect_fleet_runs(store, clock=lambda: NOW + timedelta(seconds=1))
        assert overview["active"] == []
        projection = inspect_any_run(store, run.id, clock=lambda: NOW + timedelta(seconds=1))
        incidents = {item["code"] for item in projection["doctor"]["incidents"]}
        assert {
            "CHILD_TERMINAL_PARENT_NONTERMINAL",
            "PARENT_TERMINALIZATION_MISSING",
        } <= incidents


def test_cli_recovery_is_explicit_and_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "cli-recovery.db"
    with SQLiteStore(database) as store:
        run = _run("cli-recovery-run")
        store.put("graph_run_v2", run, run_id=run.id)
        assert store.acquire_run_owner(_owner(run, acquired_at=NOW)) is None

    argv = ["recover", "cli-recovery-run", "--db", str(database)]
    assert cli.main(argv) == 0
    assert '"recovery":"recovered"' in capsys.readouterr().out
    assert cli.main(argv) == 0
    assert '"recovery":"already_recovered"' in capsys.readouterr().out


def test_sigterm_terminalizes_parent_and_records_child_cleanup(tmp_path: Path) -> None:
    import threading

    goal = Goal(id="sigterm-goal", statement="interrupt bounded work")
    graph = one_node_graph(goal, graph_id="sigterm-graph", node_id="sigterm-node")
    started = threading.Event()

    def runner(
        _node: object, request: WorkerRequest, _strategy_value: object
    ) -> NodeExecutionResult:
        started.set()
        time_limit = datetime.now(UTC) + timedelta(seconds=0.2)
        while datetime.now(UTC) < time_limit:
            threading.Event().wait(0.01)
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id="sigterm-late-result",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or ZERO,
                status="succeeded",
                duration_seconds=0.2,
            ),
            criterion_evidence=(),
        )

    def terminate_parent() -> None:
        assert started.wait(timeout=3)
        os.kill(os.getpid(), signal.SIGTERM)

    killer = threading.Thread(target=terminate_parent)
    with SQLiteStore(tmp_path / "sigterm.db") as store:
        killer.start()
        with pytest.raises(BaseException, match="SIGTERM"):
            TaskOrchestrator(store, runner, (_strategy(),)).run(
                goal,
                graph,
                ExecutionPolicy(max_nodes=1, max_attempts=1),
                harness_digest="1" * 64,
                effective_policy_digest="2" * 64,
                run_id="sigterm-run",
                available_capabilities=(),
            )
        killer.join(timeout=3)
        terminal = store.get("graph_run_v2", "sigterm-run", GraphRunRecord)
        requests = store.list_records("worker_request_v2", WorkerRequest, run_id=terminal.id)
        assert terminal.status == "interrupted"
        assert terminal.failure_code == "RUN_INTERRUPTED"
        assert store.current_run_owner(terminal.id)["status"] == "closed"  # type: ignore[index]
        assert len(requests) == 1
        assert store.control(requests[0].run_id) == "cancel"


def test_handled_orchestrator_exception_terminalizes_and_closes_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    goal = Goal(id="exception-goal", statement="fail after owner acquisition")
    graph = one_node_graph(goal, graph_id="exception-graph", node_id="exception-node")
    with SQLiteStore(tmp_path / "exception.db") as store:
        orchestrator = TaskOrchestrator(store, lambda *_args: None, (_strategy(),))

        def fail_after_running(_record: NodeExecutionRecord) -> None:
            raise RuntimeError("fixture failure")

        monkeypatch.setattr(orchestrator, "_save_node", fail_after_running)
        with pytest.raises(RuntimeError, match="fixture failure"):
            orchestrator.run(
                goal,
                graph,
                ExecutionPolicy(max_nodes=1, max_attempts=1),
                harness_digest="1" * 64,
                effective_policy_digest="2" * 64,
                run_id="exception-run",
                available_capabilities=(),
            )
        terminal = store.get("graph_run_v2", "exception-run", GraphRunRecord)
        assert terminal.status == "failed"
        assert terminal.failure_code == "ORCHESTRATOR_EXCEPTION:RuntimeError"
        assert store.current_run_owner(terminal.id)["status"] == "closed"  # type: ignore[index]


def test_killed_fake_parent_expires_without_inspector_mutation(tmp_path: Path) -> None:
    database = tmp_path / "killed-parent.db"
    script = """
import sys, time
from datetime import UTC, datetime, timedelta
from ai_employee.run_ownership import RunExecutionOwnerRecord
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import GraphRunRecord
from ai_employee.domain import ExecutionPolicy, ExecutionStrategy, Goal, RoutingMode
db = sys.argv[1]
now = datetime.now(UTC)
strategy = ExecutionStrategy(
    id='fake', routing_mode=RoutingMode.ADAPTIVE, backend='scripted', model='fixture'
)
run = GraphRunRecord(
    id='killed-run',
    goal_id='killed-goal',
    goal=Goal(id='killed-goal', statement='wait'),
    execution_policy=ExecutionPolicy(max_nodes=1, max_attempts=1),
    accepted_graph_revision_digest='0'*64,
    harness_digest='1'*64,
    effective_policy_digest='2'*64,
    available_capabilities=(),
    execution_strategies=(strategy,),
    routing_mode=RoutingMode.ADAPTIVE,
    allowed_strategy_ids=('fake',),
    allowed_backends=('scripted',),
    local_backend_allowed=False,
    status='running',
    max_concurrency=1,
    max_claims=1,
)
owner = RunExecutionOwnerRecord(
    id='killed-owner-record',
    run_id=run.id,
    created_at=now,
    graph_run_id=run.id,
    accepted_graph_revision_digest=run.accepted_graph_revision_digest,
    generation=0,
    execution_attempt=0,
    owner_instance_id='killed-owner',
    acquired_at=now,
    last_heartbeat_at=now,
    expires_at=now+timedelta(seconds=2),
    lease_duration_seconds=2,
)
with SQLiteStore(db) as store:
    store.acquire_run_owner(owner)
    store.put('graph_run_v2', run, run_id=run.id)
print('ready', flush=True)
time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(database)],
        cwd=Path.cwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        with SQLiteStore(database) as store:
            current = store.current_run_owner("killed-run")
            assert current is not None
            observed = datetime.fromisoformat(str(current["expires_at"])) + timedelta(seconds=1)
            assert inspect_fleet_runs(store, clock=lambda: observed)["active"] == []
            assert store.get("graph_run_v2", "killed-run", GraphRunRecord).status == "running"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
