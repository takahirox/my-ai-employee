"""Deterministic interruption/reopen scenarios at the authority handoff."""

import sqlite3
from datetime import timedelta

import pytest

from ai_employee.orchestration import WorkCoordinator, WorkRun
from ai_employee.process_budget import NodeProcessAdmission
from ai_employee.run_budget import RunWallBudget, WallTimeExceeded
from ai_employee.run_ownership import RunLeaseClosureRecord
from ai_employee.serialization import canonical_digest
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import TaskOrchestrator
from tests import test_closed_loop_orchestration as graph_fixture
from tests import test_work_orchestration_v2 as work_fixture
from tests.test_shared_wall_admission import Reservation


@pytest.mark.parametrize("cancel,inside_factory", [(False, False), (True, False), (False, True)])
def test_terminal_publication_reads_live_authority_after_preflight(
    tmp_path, cancel, inside_factory
):
    clock = [graph_fixture.NOW]
    database = tmp_path / "terminal.db"

    class Store(SQLiteStore):
        def terminalize_owned_graph_run(self, owner, run, closure_factory, **kwargs):
            if run.status == "completed":
                if inside_factory:
                    original = closure_factory

                    def late_factory(heartbeat):
                        clock[0] += timedelta(seconds=61)
                        return original(heartbeat)

                    closure_factory = late_factory
                else:
                    clock[0] += timedelta(seconds=61)
                if cancel:
                    with SQLiteStore(database) as writer:
                        writer.request_control(run.id, "cancel")
            return super().terminalize_owned_graph_run(owner, run, closure_factory, **kwargs)

    goal, graph, _ = graph_fixture._inputs()
    with Store(database) as store:
        scheduler = TaskOrchestrator(
            store,
            lambda _node, request, _strategy: graph_fixture._result(request, "satisfied"),
            (graph_fixture._strategy(),),
            clock=lambda: clock[0],
            wall_clock=lambda: clock[0].timestamp(),
            lease_duration_seconds=120.0,
        )
        result = scheduler.run(
            goal,
            graph,
            graph_fixture.ExecutionPolicy(max_nodes=1, max_attempts=4, max_wall_seconds=60.0),
            run_id="terminal",
            harness_digest=graph_fixture.HARNESS,
            effective_policy_digest=graph_fixture.POLICY,
            available_capabilities=("process",),
        )
        assert result.status == ("cancelled" if cancel else "failed")
        assert result.failure_code == ("GRAPH_CANCELLED" if cancel else "RUN_WALL_BUDGET_EXCEEDED")
        closures = store.list_records(
            "run_lease_closure_v2", RunLeaseClosureRecord, run_id=result.id
        )
        assert len(closures) == 1
        assert closures[0].terminal_graph_status == result.status
        changes = store._connection.total_changes
        store._connection.execute("PRAGMA query_only=ON")
        assert scheduler.replay(result.id).run == result
        assert store._connection.total_changes == changes


def test_admission_uses_live_budget_and_rejects_foreign_authority(tmp_path):
    clock = [0.0]
    budget = RunWallBudget("run", 10.0, 0.0, 0.0, lambda: clock[0])
    with SQLiteStore(tmp_path / "admission.db") as store:

        def reserve():
            return store.reserve_graph_node(
                "run",
                "node",
                0,
                0,
                max_claims=1,
                worker_turns=1,
                processes=1,
                wall_seconds=10.0,
                artifact_bytes=10,
                limits={
                    "wall_seconds": 10.0,
                    "worker_turns": 1,
                    "processes": 1,
                    "artifact_bytes": 10,
                },
                record_factory=lambda remaining: Reservation(id="reservation", remaining=remaining),
                wall_budget=budget,
            )

        # Simulate elapsed time on entry into the write transaction after preflight.
        assert budget.remaining_seconds == 10.0

        def trace(statement):
            if statement == "BEGIN IMMEDIATE":
                clock[0] = 10.0

        store._connection.set_trace_callback(trace)
        with pytest.raises(WallTimeExceeded):
            reserve()
        assert store.list_records("node_reservation_v2", Reservation, run_id="run") == ()
        store._connection.set_trace_callback(None)
        budget.run_id = "foreign"
        with pytest.raises(ValueError, match="wall authority"):
            reserve()


def _work_run():
    return WorkRun(
        id="work",
        goal="bounded fixture",
        repository="fixture",
        base_commit="a" * 40,
        worker="scripted",
        effective_policy_digest=canonical_digest([]),
        capture_patch=False,
        status="paused",
        completed_action_digests=("1" * 64,),
    )


def test_work_checkpoint_is_an_atomic_projection_across_crash_and_reopen(tmp_path):
    path = tmp_path / "checkpoint.db"
    original = _work_run()
    with SQLiteStore(path) as store:
        store.save_work_run(original)
        store._connection.execute("""CREATE TRIGGER interrupt_checkpoint
            BEFORE INSERT ON work_checkpoints_v2
            BEGIN SELECT RAISE(ABORT, 'simulated crash'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="simulated crash"):
            store.save_work_run(original.model_copy(update={"generation": 1, "status": "running"}))
    with SQLiteStore(path) as store:
        assert store.get_work_run(original.id) == original
        generation, checkpoint = store.load_work_checkpoint(original.id)
        assert generation == 0
        assert checkpoint == {
            "status": "paused",
            "policy_digest": original.effective_policy_digest,
            "completed_action_digests": list(original.completed_action_digests),
        }
        store._connection.execute("DROP TRIGGER interrupt_checkpoint")
        updated = original.model_copy(update={"generation": 1, "status": "completed"})
        store.save_work_run(updated)
        assert store.load_work_checkpoint(original.id)[0] == 1


@pytest.mark.parametrize("completed_receipt", [False, True])
def test_crashed_effect_without_committed_completion_never_reexecutes(tmp_path, completed_receipt):
    """Use the real coordinator's start/effect path and interrupt result persistence."""
    f = work_fixture
    database = tmp_path / "effect.db"
    calls = []
    proposal = f.ActionProposal(
        id="proposal",
        run_id="work",
        created_at=f.NOW,
        worker_id="scripted",
        kind=f.ActionKind.PROCESS,
        reason="write disposable marker",
        payload=f.ProcessRequest(
            id="effect",
            run_id="work",
            created_at=f.NOW,
            argv=("fixture-effect",),
            purpose="bounded fixture",
        ),
    )

    class Crash(BaseException):
        pass

    class Store(SQLiteStore):
        def put(self, kind, model, **kwargs):
            if kind == "action_result_v2" and not completed_receipt:
                raise Crash()
            return super().put(kind, model, **kwargs)

        def save_work_run(self, run):
            if completed_receipt and run.completed_action_digests:
                raise Crash()
            return super().save_work_run(run)

    class Executor(f.SuccessfulExecutor):
        def execute(self, request, decision, cancellation):
            calls.append(request.id)
            (tmp_path / "effect").write_text("occurred")
            return super().execute(request, decision, cancellation)

    policy = f.builtin_policy("work").model_copy(
        update={"allowed_capabilities": ("process",), "content_digest": None}
    )

    def coordinator(store):
        return WorkCoordinator(
            store,
            f.DeterministicRuntime({}, store=store),
            f.FakeWorkspace(b""),
            lambda *_: f.ScriptedWorkerAdapter([f.WorkerProposalEnvelope(proposals=(proposal,))]),
            lambda _: Executor(),
            lambda _: b"",
            (policy,),
            allowed_processes=(("fixture-effect",),),
            request_promotion_approval=False,
        )

    with Store(database) as store, pytest.raises(Crash):
        coordinator(store).start(
            "bounded effect",
            str(tmp_path),
            "a" * 40,
            run_id="work",
            worker_name="scripted",
            _capture_patch=False,
            _accepted_request=f.WorkerRequest(
                id="accepted",
                run_id="work",
                created_at=f.NOW,
                goal="bounded effect",
                node_id="node",
                graph_run_id="graph",
                accepted_graph_revision_digest=f.ZERO,
                accepted_plan_digest=f.ZERO,
                harness_digest=f.ZERO,
                effective_policy_digest=canonical_digest([policy.content_digest]),
                remaining_budgets={"worker_turns": 1, "processes": 1, "artifact_bytes": 1000},
            ),
        )
    assert calls == ["effect"]
    assert (tmp_path / "effect").read_text() == "occurred"
    with SQLiteStore(database) as store:
        admissions = store.list_records(
            "node_process_admission_v2", NodeProcessAdmission, run_id="work"
        )
        assert sum(item.units for item in admissions) == 1
        result = coordinator(store).resume("work")
        assert result.status == "failed"
        assert result.failure_code == "ACTION_OUTCOME_UNKNOWN"
        events = store.work_events("work")
        assert coordinator(store).resume("work") == result
        assert store.work_events("work") == events
        assert (
            store.list_records("node_process_admission_v2", NodeProcessAdmission, run_id="work")
            == admissions
        )
    assert calls == ["effect"]


def test_expired_reservation_factory_rolls_back_all_claims(tmp_path):
    clock = [0.0]
    budget = RunWallBudget("run", 10.0, 0.0, 0.0, lambda: clock[0])
    with SQLiteStore(tmp_path / "rollback.db") as store:

        def expired_factory(remaining):
            clock[0] = 10.0
            return Reservation(id="reservation", remaining=remaining)

        with pytest.raises(WallTimeExceeded):
            store.reserve_graph_node(
                "run",
                "node",
                0,
                0,
                max_claims=1,
                worker_turns=1,
                processes=1,
                wall_seconds=10.0,
                artifact_bytes=10,
                limits={
                    "wall_seconds": 10.0,
                    "worker_turns": 1,
                    "processes": 1,
                    "artifact_bytes": 10,
                },
                record_factory=expired_factory,
                wall_budget=budget,
            )
        for table in ("graph_reservations_v2", "graph_claims_v2"):
            assert store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert store.list_records("node_reservation_v2", Reservation, run_id="run") == ()
