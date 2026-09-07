from datetime import timedelta

import pytest

import ai_employee.task_orchestration as scheduling
from ai_employee.domain.v2 import ProcessRequest
from ai_employee.run_budget import (
    RunWallFinish,
    RunWallStart,
    WallTimeExceeded,
    current_wall_budget,
    remaining_timeout,
    wall_budget_scope,
)
from ai_employee.stage_control import StageCancellation
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import TaskOrchestrator
from tests import test_closed_loop_orchestration as fixture


def test_nested_stages_share_budget_and_resume_excludes_paused_wait(tmp_path):
    clock = [0.0]

    def utc():
        return fixture.NOW + timedelta(seconds=clock[0])

    with SQLiteStore(tmp_path / "budget.db") as store:
        with wall_budget_scope(store, "run", 10.0, clock=lambda: clock[0], utc_clock=utc) as budget:
            clock[0] += 3.0
            assert remaining_timeout(20.0) == 7.0
            with wall_budget_scope(store, "run", 100.0) as nested:
                assert nested is budget
                clock[0] += 4.0
                assert remaining_timeout(20.0) == 3.0
        clock[0] = 100.0
        with wall_budget_scope(
            store, "run", 100.0, clock=lambda: clock[0], utc_clock=utc
        ) as resumed:
            assert resumed.remaining_seconds == 3.0
            assert resumed.limit == 10.0
            clock[0] += 3.0
            with pytest.raises(WallTimeExceeded):
                remaining_timeout(20.0)
            assert StageCancellation().cancelled()
        assert len(store.list_records("run_wall_start_v2", RunWallStart, run_id="run")) == 2
        assert sorted(
            r.active_seconds
            for r in store.list_records("run_wall_finish_v2", RunWallFinish, run_id="run")
        ) == [3.0, 7.0]
    assert current_wall_budget() is None
    assert not StageCancellation().cancelled()


def test_recovery_charges_unclosed_interval_once_without_resetting_allowance(tmp_path):
    clock = [6.0]

    def utc():
        return fixture.NOW + timedelta(seconds=clock[0])

    with SQLiteStore(tmp_path / "budget.db") as store:
        start = RunWallStart(
            id="crashed-start",
            invocation_id="crashed-invocation",
            graph_run_id="run",
            started_at=fixture.NOW,
            run_id="run",
            created_at=fixture.NOW,
            limit_seconds=10.0,
        )
        store.put_once("run_wall_start_v2", start, run_id="run")
        with wall_budget_scope(store, "run", 10.0, clock=lambda: clock[0], utc_clock=utc) as budget:
            assert budget.remaining_seconds == 4.0
            clock[0] += 1.0
        clock[0] = 100.0
        with wall_budget_scope(
            store, "run", 10.0, clock=lambda: clock[0], utc_clock=utc
        ) as resumed:
            assert resumed.remaining_seconds == 3.0
        receipts = store.list_records("run_wall_finish_v2", RunWallFinish, run_id="run")
        assert sum(r.recovered_interval for r in receipts) == 1


@pytest.mark.parametrize("with_profile", [False, True])
def test_legacy_consumption_is_imported_once_from_bound_runtime_records(tmp_path, with_profile):
    from ai_employee.domain import ProjectHarnessV2, RoutingMode
    from ai_employee.execution_profile import ProfileTiming, choose_profile
    from ai_employee.run_ownership import RunLeaseClosureRecord
    from tests.test_issue57_run_ownership import _owner, _run

    run = _run("run")
    owner = _owner(run)
    clock = [100.0]
    with SQLiteStore(tmp_path / "legacy.db") as store:
        assert store.acquire_run_owner(owner) is None
        assert store.put_owned_graph_run(owner, run, observed_at=owner.acquired_at)
        closed_at = owner.acquired_at + timedelta(seconds=4)
        closure = store.terminalize_owned_graph_run(
            owner,
            run.model_copy(update={"status": "paused"}),
            lambda heartbeat: RunLeaseClosureRecord(
                id="legacy-close",
                run_id=run.id,
                created_at=closed_at,
                graph_run_id=run.id,
                accepted_graph_revision_digest=run.accepted_graph_revision_digest,
                generation=0,
                execution_attempt=0,
                owner_instance_id=owner.owner_instance_id,
                owner_record_id=owner.id,
                owner_record_digest=owner.content_digest,
                final_heartbeat_digest=heartbeat,
                closed_at=closed_at,
                terminal_graph_status="paused",
                reason="pause",
            ),
            observed_at=closed_at,
        )
        assert closure is not None
        if with_profile:
            profile = choose_profile(
                "run", ProjectHarnessV2(), "1" * 64, RoutingMode.FIXED, "baseline"
            )
            store.put_once("execution_profile_v2", profile, run_id="run")
            for phase, seconds in (("invocation_start", 1.0), ("invocation", 7.0)):
                store.put_once(
                    "execution_profile_timing_v2",
                    ProfileTiming(
                        id="legacy-" + phase,
                        run_id="run",
                        created_at=owner.acquired_at + timedelta(seconds=seconds),
                        profile_digest=profile.content_digest,
                        phase=phase,
                        seconds=seconds,
                    ),
                    run_id="run",
                )
        prior = 7.0 if with_profile else 4.0
        for iteration in range(2):
            with wall_budget_scope(
                store,
                "run",
                10.0,
                clock=lambda: clock[0],
                utc_clock=lambda: owner.acquired_at + timedelta(seconds=clock[0]),
            ) as budget:
                assert budget.remaining_seconds == 10.0 - prior - iteration
                clock[0] += 1.0
            clock[0] += 100.0
        imported = [
            r
            for r in store.list_records("run_wall_start_v2", RunWallStart, run_id="run")
            if r.legacy_sources
        ]
        assert len(imported) == 1
        assert owner.content_digest in imported[0].legacy_sources
        assert closure.content_digest in imported[0].legacy_sources


@pytest.mark.parametrize("cancel", [False, True])
def test_late_task_review_cannot_complete_after_global_budget(tmp_path, monkeypatch, cancel):
    clock = [fixture.NOW]
    monkeypatch.setattr(scheduling, "now", lambda: clock[0])
    goal, graph, node = fixture._inputs()
    node = node.model_copy(
        update={"resource_budget": node.resource_budget.model_copy(update={"wall_seconds": 30.0})}
    )
    graph = graph.model_copy(
        update={
            "nodes": (node,),
            "budget": graph.budget.model_copy(update={"max_wall_seconds": 60.0}),
        }
    )
    with SQLiteStore(tmp_path / "review.db") as store:

        class Reviewer(fixture._ScriptedTaskReviewer):
            def review(self, request):
                clock[0] += timedelta(seconds=80)
                if cancel:
                    store.request_control("late-review", "cancel")
                return super().review(request)

        reviewer = Reviewer({})
        orchestrator = TaskOrchestrator(
            store,
            lambda _node, request, _strategy: fixture._result(request, "satisfied"),
            (fixture._strategy(),),
            task_reviewer=reviewer,
            independent_task_review=True,
            clock=lambda: clock[0],
            wall_clock=lambda: clock[0].timestamp(),
            lease_duration_seconds=120.0,
        )
        run = orchestrator.run(
            goal,
            graph,
            fixture.ExecutionPolicy(max_nodes=1, max_attempts=4, max_wall_seconds=60.0),
            harness_digest=fixture.HARNESS,
            effective_policy_digest=fixture.POLICY,
            run_id="late-review",
            available_capabilities=("process",),
        )
        assert run.status == ("cancelled" if cancel else "failed")
        assert run.failure_code == ("GRAPH_CANCELLED" if cancel else "RUN_WALL_BUDGET_EXCEEDED")
        assert not orchestrator.replay(run.id).task_review_results
        assert store.current_run_owner(run.id)["status"] == "closed"


def test_process_uses_remaining_budget_without_mutating_bound_request(tmp_path):
    import sys
    from pathlib import Path

    from ai_employee.domain.v2 import DecisionOutcome, PolicyDecision, StableFailureCode
    from ai_employee.services_v2 import AtomicArtifactStore, LocalProcessExecutor

    clock = [0.0]
    with SQLiteStore(tmp_path / "process.db") as store:
        executor = LocalProcessExecutor(
            (tmp_path,),
            AtomicArtifactStore(tmp_path / "artifacts"),
            executable_paths=(Path(sys.executable).resolve().parent,),
        )
        request = ProcessRequest(
            id="process",
            run_id="run",
            created_at=fixture.NOW,
            argv=(sys.executable, "-c", "import time; time.sleep(5)"),
            timeout_seconds=10.0,
            purpose="bounded offline process deadline probe",
        )
        decision = PolicyDecision(
            id="allow",
            run_id="run",
            created_at=fixture.NOW,
            request_digest=request.content_digest,
            effective_policy_digest=fixture.POLICY,
            outcome=DecisionOutcome.ALLOW,
            reason_code="test_process",
        )
        with wall_budget_scope(
            store, "run", 10.0, clock=lambda: clock[0], utc_clock=lambda: fixture.NOW
        ):
            clock[0] = 9.95
            result = executor.execute(request, decision, StageCancellation())
        assert request.timeout_seconds == 10.0
        assert result.request_digest == request.content_digest
        assert result.failure.code is StableFailureCode.TIMEOUT
        assert result.duration_seconds < 2.0


def test_accounting_identity_and_clock_origin_are_digest_bound():
    record = RunWallStart(
        id="start",
        invocation_id="invocation",
        graph_run_id="run",
        run_id="run",
        created_at=fixture.NOW,
        started_at=fixture.NOW,
        limit_seconds=10.0,
    )
    for changes in (
        {"run_id": "other", "graph_run_id": "other"},
        {
            "started_at": fixture.NOW + timedelta(seconds=5),
            "created_at": fixture.NOW + timedelta(seconds=5),
        },
        {"invocation_id": "different"},
    ):
        with pytest.raises(ValueError, match="content_digest"):
            RunWallStart.model_validate({**record.model_dump(), **changes}, strict=True)
    assert RunWallStart.model_validate_json(record.model_dump_json(), strict=True) == record


@pytest.mark.parametrize("limit", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_budget_cannot_mutate_an_active_scope(tmp_path, limit):
    with (
        SQLiteStore(tmp_path / "budget.db") as store,
        wall_budget_scope(store, "run", 10.0) as budget,
    ):
        with (
            pytest.raises(ValueError, match="positive and finite"),
            wall_budget_scope(store, "run", limit),
        ):
            raise AssertionError("invalid nested budget admitted")
        assert budget.limit == 10.0


@pytest.mark.parametrize("normal_first", [False, True])
@pytest.mark.parametrize("normal_seconds", [2.0, 5.0])
def test_normal_finalization_and_recovery_settle_one_interval(
    tmp_path, normal_first, normal_seconds
):
    import threading

    database = tmp_path / "settlement.db"
    pending = threading.Event()
    release = threading.Event()
    errors = []
    clock = [0.0]

    class DelayedStore(SQLiteStore):
        def put_once(self, kind, model, **kwargs):
            if kind == "run_wall_finish_v2" and not model.recovered_interval:
                if normal_first:
                    super().put_once(kind, model, **kwargs)
                pending.set()
                assert release.wait(10)
            return super().put_once(kind, model, **kwargs)

    def first():
        try:
            with (
                DelayedStore(database) as store,
                wall_budget_scope(
                    store,
                    "run",
                    10.0,
                    clock=lambda: clock[0],
                    utc_clock=lambda: fixture.NOW,
                ),
            ):
                clock[0] = normal_seconds
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=first)
    thread.start()
    assert pending.wait(10)
    try:
        with (
            SQLiteStore(database) as store,
            wall_budget_scope(
                store,
                "run",
                10.0,
                clock=lambda: 0.0,
                utc_clock=lambda: fixture.NOW + timedelta(seconds=3),
            ) as budget,
        ):
            assert budget.remaining_seconds == (10.0 - normal_seconds if normal_first else 7.0)
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert not errors
    with SQLiteStore(database) as store:
        receipts = store.list_records("run_wall_finish_v2", RunWallFinish, run_id="run")
        assert len({item.start_digest for item in receipts}) == 2
        assert len(receipts) == (2 if normal_first else 3)
        assert sum(not item.recovered_interval for item in receipts) == 2
        with wall_budget_scope(store, "run", 10.0) as resumed:
            assert resumed.prior == (normal_seconds if normal_first else max(normal_seconds, 3.0))


def test_historical_normal_recovery_overlap_is_charged_once_conservatively(tmp_path):
    with SQLiteStore(tmp_path / "legacy-race.db") as store:
        start = RunWallStart(
            id="start",
            invocation_id="invocation",
            graph_run_id="run",
            run_id="run",
            created_at=fixture.NOW,
            started_at=fixture.NOW,
            limit_seconds=10.0,
        )
        store.put_once("run_wall_start_v2", start, run_id="run")
        for name, seconds, recovered in (("normal", 2.0, False), ("recovery", 3.0, True)):
            finish = RunWallFinish(
                id=name,
                graph_run_id="run",
                run_id="run",
                created_at=fixture.NOW,
                start_digest=start.content_digest,
                limit_seconds=10.0,
                active_seconds=seconds,
                recovered_interval=recovered,
            )
            store.put_once("run_wall_finish_v2", finish, run_id="run")
        with wall_budget_scope(store, "run", 10.0) as budget:
            assert budget.prior == 3.0
        bad = finish.model_dump()
        bad.update(id="conflicting-normal", recovered_interval=False, content_digest=None)
        store.put_once("run_wall_finish_v2", RunWallFinish.model_validate(bad), run_id="run")
        with (
            pytest.raises(ValueError, match="conflicting completion receipts"),
            wall_budget_scope(store, "run", 10.0),
        ):
            pass


@pytest.mark.parametrize("expire", [False, True])
def test_isolated_verification_receives_shared_deadline_and_rejects_late_success(
    tmp_path,
    monkeypatch,
    expire,
):
    import ai_employee.isolated_execution as isolated
    from ai_employee.domain.v2 import DecisionOutcome, PolicyDecision
    from ai_employee.isolated_worker import IsolatedWorkerProfile
    from ai_employee.services_v2 import AtomicArtifactStore

    clock = [0.0]
    observations = {}

    class Container:
        def __init__(self, profile, root, *, seconds, cancellation, **kwargs):
            observations["seconds"] = seconds
            self.cancellation = cancellation
            self.native_process_usage = {"admitted": 1}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            observations["cleaned"] = True

        def run_guarded(self, argv, process_limit):
            clock[0] += 2.0 if expire else 0.1
            observations["cancelled"] = self.cancellation.cancelled()
            return 0, b"", b""

    monkeypatch.setattr(isolated, "DockerCandidate", Container)
    executor = isolated.DockerProcessExecutor(
        (tmp_path,), AtomicArtifactStore(tmp_path / "artifacts")
    )
    executor.profile = IsolatedWorkerProfile(image="sha256:" + "1" * 64)
    request = ProcessRequest(
        id="verify",
        run_id="run",
        created_at=fixture.NOW,
        argv=("python", "-c", "pass"),
        timeout_seconds=20.0,
        purpose="verify shared deadline",
    )
    decision = PolicyDecision(
        id="allow",
        run_id="run",
        created_at=fixture.NOW,
        request_digest=request.content_digest,
        effective_policy_digest=fixture.POLICY,
        outcome=DecisionOutcome.ALLOW,
        reason_code="fixture",
    )
    with (
        SQLiteStore(tmp_path / "isolated.db") as store,
        wall_budget_scope(store, "run", 10.0, clock=lambda: clock[0]),
    ):
        clock[0] = 9.0
        if expire:
            with pytest.raises(WallTimeExceeded):
                executor.execute(request, decision, StageCancellation())
        else:
            result = executor.execute(request, decision, StageCancellation())
            assert result.status == "succeeded"
            assert result.request_digest == request.content_digest
    assert observations == {"seconds": 1.0, "cancelled": expire, "cleaned": True}
    assert request.timeout_seconds == 20.0
