"""Wall allowance is shared by admission, invocation and stop handling."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from ai_employee.container import ContainerModel
from ai_employee.history import Journal, Stopped
from ai_employee.isolated_worker import IsolatedWorkerProfile
from ai_employee.models import Authority, Check, Clarification, StagePolicy, Usage

from .test_autonomous_runtime import OfflineModel, clarification, config, runtime
from .test_stage_contracts import stream


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 1000.0

        def advance(self, seconds):
            self.now += seconds

    result = Clock()
    monkeypatch.setattr(time, "time", lambda: result.now)
    monkeypatch.setattr(time, "monotonic", lambda: result.now)
    return result


def test_short_wall_and_reopen_do_not_get_full_invocation_budget(tmp_path, clock):
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config(wall_seconds=10, invocation_seconds=300))
    first, seconds = journal.reserve(run, "planning")
    assert seconds == 10
    clock.advance(4)
    journal.settle(run, first, 4, Usage(tokens=12, cost=0))
    journal = Journal(journal.path)
    second, seconds = journal.reserve(run, "worker")
    assert seconds == 6
    clock.advance(6)
    journal.settle(run, second, 6, Usage())
    with pytest.raises(Stopped, match="WALL_BUDGET_EXHAUSTED"):
        journal.reserve(run, "verification")
    assert journal.budget(run)["invocations"] == 2
    assert journal.budget(run)["measured_usage"]["tokens"] is None
    assert journal.budget(run)["open_reservations"] == 0


@pytest.mark.parametrize("counts", [False, True])
def test_overlapping_approval_waits_share_wall_definition_on_resume(tmp_path, clock, counts):
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config(wall_seconds=20, approval_counts_wall=counts))
    clock.advance(2)
    journal.append(run, "approval_wait", attempt="a")
    clock.advance(2)
    journal.append(run, "approval_wait", attempt="b")
    clock.advance(3)
    journal.append(run, "authority_rejected", attempt="a")
    clock.advance(3)
    journal = Journal(journal.path)
    assert journal.remaining_wall(run) == (10 if counts else 18)
    journal.append(run, "authority_rejected", attempt="b")
    clock.advance(3)
    _, seconds = journal.reserve(run, "planning")
    assert seconds == (7 if counts else 15)
    clock.advance(seconds)
    with pytest.raises(Stopped, match="WALL_BUDGET_EXHAUSTED"):
        journal.check(run)


def test_parallel_reservations_share_active_but_not_additive_wall(tmp_path, clock):
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config(wall_seconds=10, active_seconds=15))
    with ThreadPoolExecutor(max_workers=2) as pool:
        reserved = list(pool.map(lambda _: journal.reserve(run, "worker"), range(2)))
    assert sorted(seconds for _, seconds in reserved) == [5, 10]
    assert journal.remaining_wall(run) == 10
    with pytest.raises(Stopped, match="RUN_BUDGET_EXHAUSTED"):
        journal.reserve(run, "worker")


def test_wall_rechecked_after_reservation_lock(tmp_path, clock, monkeypatch):
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config(wall_seconds=10))
    guard = journal._decision_guard

    def delayed(db, run):
        guard(db, run)
        clock.advance(11)

    monkeypatch.setattr(journal, "_decision_guard", delayed)
    with pytest.raises(Stopped, match="WALL_BUDGET_EXHAUSTED"):
        journal.reserve(run, "worker")
    assert journal.budget(run)["invocations"] == 0


class TimedModel(OfflineModel):
    def __init__(self, clock, *, preflight=3, failure=None):
        super().__init__()
        self.clock, self.preflight_seconds, self.failure = clock, preflight, failure
        self.timeouts = []
        self.engine = None
        self.run = None

    def preflight(self, policy, workspace, authority, timeout, cancelled, checks=()):
        self.timeouts.append(("preflight", timeout))
        self.clock.advance(self.preflight_seconds)
        return {"available": True}

    def generate(self, policy, prompt, schema, workspace, authority, timeout, cancelled, **kwargs):
        body = json.loads(prompt)
        assert timeout == body["execution_budget"]["reserved_active_seconds"]
        self.timeouts.append(("model", timeout))
        if self.failure:
            if self.failure in ("USAGE_LIMIT", "OPERATOR_CANCELLED"):
                self.engine.journal.stop(self.run, self.failure)
            else:
                self.clock.advance(timeout)
            raise TimeoutError("interrupted native process")
        return clarification(), Usage(tokens=12, cost=0)


def invoke(tmp_path, model, **limits):
    engine, source = runtime(tmp_path, model)
    cfg = config(wall_seconds=10, **limits)
    run = engine.prepare("Write result", cfg, source)
    model.engine, model.run = engine, run
    return engine, source, cfg, run


def test_preflight_consumption_reaches_prompt_timeout_and_diagnostics(tmp_path, clock):
    model = TimedModel(clock)
    engine, _source, cfg, run = invoke(tmp_path, model)
    tree = next(e["body"]["tree"] for e in engine.journal.events(run) if e["kind"] == "input")
    goal = engine._clarify(run, cfg, tree)
    assert goal.specification.disposition == "proceed"
    assert model.timeouts == [("preflight", 10), ("model", 7)]
    budgets = [
        e["body"]["observation"]
        for e in engine.journal.events(run)
        if e["kind"] == "worker_observation"
    ]
    assert budgets == [{"event": "execution_budget", "phase": "after_preflight", "seconds": 7}]


def test_exhausted_preflight_never_launches_model_and_settles_zero_tokens(tmp_path, clock):
    model = TimedModel(clock, preflight=10)
    engine, _source, _cfg, run = invoke(tmp_path, model)
    with pytest.raises(Stopped, match="WALL_BUDGET_EXHAUSTED"):
        engine.execute(run)
    assert model.timeouts == [("preflight", 10)]
    budget = engine.journal.budget(run)
    assert budget["open_reservations"] == 0 and budget["measured_usage"]["tokens"] == 0
    assert not any(
        e["kind"] in ("failed", "transport_failed", "goal") for e in engine.journal.events(run)
    )


@pytest.mark.parametrize("reason", ["WALL_BUDGET_EXHAUSTED", "USAGE_LIMIT", "OPERATOR_CANCELLED"])
@pytest.mark.parametrize("external", [False, True])
def test_timeout_cannot_hide_stop_or_retry_and_external_uncertainty_is_retained(
    tmp_path, clock, reason, external
):
    model = TimedModel(clock, failure=reason)
    engine, source, _cfg, run = invoke(tmp_path, model)
    policy = StagePolicy(model="test", transport_retries=2)
    with pytest.raises(Stopped, match=reason):
        engine._generate(
            run,
            "clarification",
            policy,
            {"original_input": "Write result"},
            Clarification,
            source,
            Authority(external_writes=external),
        )
    events = engine.journal.events(run)
    assert len(model.timeouts) == 2
    assert sum(e["kind"] == "reserved" for e in events) == 1
    assert any(e["kind"] == "uncertain" for e in events) == external
    assert not any(e["kind"] in ("stage_result", "output_rejected", "failed") for e in events)
    assert engine.journal.budget(run)["open_reservations"] == 0
    assert engine.journal.budget(run)["measured_usage"]["tokens"] is None


@pytest.mark.parametrize("setup", [4, 10])
def test_native_setup_reduces_prompt_budget_and_prevents_expired_launch(tmp_path, clock, setup):
    model = ContainerModel(
        IsolatedWorkerProfile(image="sha256:" + "a" * 64, auth_file="/test-auth")
    )
    candidate = MagicMock()
    candidate.deadline = clock.now + 10
    candidate.proxy = None
    candidate.run_guarded.return_value = (
        0,
        stream(clarification().model_dump(mode="json")).encode(),
        b"",
    )
    observations = []
    with (
        patch.object(model, "_candidate") as factory,
        patch.object(model, "_native_probe", side_effect=lambda *args: clock.advance(setup)),
        patch.object(model, "_copy_workspace"),
    ):
        factory.return_value.__enter__.return_value = candidate
        args = (
            config().clarification,
            json.dumps({"execution_budget": {"reserved_active_seconds": 10}}),
            Clarification,
            tmp_path,
            Authority(),
            10,
            lambda: False,
        )
        if setup == 10:
            with pytest.raises(TimeoutError, match="NATIVE_SETUP_TIMEOUT"):
                model.generate(*args, observation=observations.append)
            candidate.run_guarded.assert_not_called()
        else:
            model.generate(*args, observation=observations.append)
            body = json.loads(candidate.run_guarded.call_args.kwargs["stdin"])
            assert body["execution_budget"]["reserved_active_seconds"] == 6
            assert observations[-1] == {
                "event": "execution_budget",
                "phase": "native_launch",
                "seconds": 6,
            }


@pytest.mark.parametrize("interrupted", [False, True])
def test_protected_check_shares_wall_and_cannot_admit_late_receipt(tmp_path, clock, interrupted):
    class CheckModel(OfflineModel):
        def check(self, argv, workspace, timeout, cancelled):
            assert timeout == 10
            clock.advance(timeout)
            if interrupted:
                raise TimeoutError("check deadline")
            return True, "late passing output"

    engine, source = runtime(tmp_path, CheckModel())
    cfg = config(wall_seconds=10).model_copy(
        update={
            "checks": (Check(id="protected", argv=("true",)),),
            "mandatory_checks": ("protected",),
        }
    )
    # Supply required check in clarification without changing the rest of the runtime.
    original = engine.model.generate

    def generate(policy, prompt, schema, *args, **kwargs):
        result, usage = original(policy, prompt, schema, *args, **kwargs)
        if schema is Clarification:
            result = result.model_copy(
                update={
                    "criteria": tuple(
                        c.model_copy(update={"checks": ("protected",)}) for c in result.criteria
                    )
                }
            )
        return result, usage

    engine.model.generate = generate
    with pytest.raises(Stopped, match="WALL_BUDGET_EXHAUSTED"):
        engine.start("Write result", cfg, source)
    run = engine.journal.runs()[0]
    events = engine.journal.events(run)
    assert not any(e["kind"] in ("check_result", "completed", "failed") for e in events)
    assert engine.journal.budget(run)["open_reservations"] == 0
    assert any(e["kind"] == "diagnostic" and e["body"]["stage"] == "check" for e in events)


@pytest.mark.parametrize("stage", ["check", "authority_application"])
def test_nonmodel_reservations_use_wall_without_model_usage(tmp_path, clock, stage):
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config(wall_seconds=10, tokens=100000, cost=10))
    clock.advance(8)
    reservation, seconds = journal.reserve(run, stage, model_usage=False)
    assert seconds == 2
    event = journal.events(run)[-1]["body"]
    assert event["tokens"] == 0 and event["cost"] == 0
    journal.settle(run, reservation, 1, Usage(tokens=0, cost=0))
    assert journal.budget(run)["measured_usage"] == {"tokens": 0, "cost": 0.0}
