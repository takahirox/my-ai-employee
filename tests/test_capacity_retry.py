"""Recorded Codex event shape through ContainerModel, Engine, and durable retries."""

import json
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from ai_employee.cli import main, projection
from ai_employee.container import ContainerModel
from ai_employee.engine import Engine, Waiting
from ai_employee.history import Journal, Stopped
from ai_employee.isolated_worker import IsolatedWorkerProfile
from ai_employee.models import Authority, Clarification, RunConfig, StagePolicy
from ai_employee.native import capacity_error

from .test_autonomous_runtime import OfflineModel, clarification, config, runtime
from .test_stage_contracts import stream

MESSAGE = "Selected model is at capacity. Please try a different model."
CAPACITY = (
    json.dumps({"type": "error", "message": MESSAGE})
    + "\n"
    + json.dumps({"type": "turn.failed", "error": {"message": MESSAGE}})
)


@pytest.mark.parametrize(
    "event",
    [
        {"type": "error", "message": MESSAGE},
        {"type": "item.completed", "item": {"type": "agent_message", "text": MESSAGE}},
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "aggregated_output": CAPACITY},
        },
        {"type": "turn.failed", "error": {"message": "Authentication failed"}},
        {"type": "turn.failed", "error": {"message": "You have hit your usage limit."}},
        {"type": "turn.failed", "error": {"message": "unknown"}},
        {"type": "turn.failed", "error": MESSAGE},
        None,
    ],
)
def test_only_known_terminal_capacity_is_classified(event):
    assert not capacity_error(json.dumps(event))
    assert not capacity_error("malformed")
    assert capacity_error(CAPACITY)
    assert not capacity_error(CAPACITY + '\n{"type":"turn.completed"}')


class BoundaryModel(OfflineModel):
    def __init__(self, failures, output=CAPACITY):
        super().__init__()
        self.failures, self.output = failures, output
        self.native_calls = 0
        self.cleaned = 0
        self.commands = []

    def generate(self, policy, prompt, schema, workspace, authority, timeout, cancelled, **kw):
        if schema is not Clarification:
            return super().generate(
                policy, prompt, schema, workspace, authority, timeout, cancelled
            )
        assert self.cleaned == self.native_calls
        self.native_calls += 1
        profile = IsolatedWorkerProfile(
            image="sha256:" + "a" * 64, auth_file=str(workspace / "fixture-auth")
        )
        native = ContainerModel(profile)
        candidate = MagicMock(profile=profile, deadline=None, proxy=None, name="fixture")
        candidate.name = "fixture"
        failed = self.native_calls <= self.failures
        candidate.run_guarded.return_value = (
            int(failed),
            (self.output if failed else stream(clarification().model_dump())).encode(),
            b"",
        )

        @contextmanager
        def owned(*args, **kwargs):
            try:
                yield candidate
            finally:
                self.cleaned += 1

        with (
            patch.object(native, "_candidate", owned),
            patch.object(native, "_native_probe"),
            patch.object(native, "_copy_workspace"),
        ):
            try:
                return native.generate(
                    policy, prompt, schema, workspace, authority, timeout, cancelled, **kw
                )
            finally:
                if candidate.run_guarded.called:
                    self.commands.append(candidate.run_guarded.call_args.args[0])


@pytest.fixture
def clock(monkeypatch):
    now = [time.time()]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(time, "sleep", sleep)
    return now, sleeps


def test_fresh_init_retries_native_capacity_and_preserves_identity(tmp_path, clock):
    output = tmp_path / "config.json"
    assert (
        main(
            [
                "init",
                "--model",
                "test-only",
                "--image",
                "sha256:" + "a" * 64,
                "--auth-file",
                "fixture",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    cfg = RunConfig.model_validate_json(output.read_text())
    assert cfg.clarification.transport_retries == 2
    # This fixture does not supply optional review responses.
    cfg = cfg.model_copy(
        update={"clarification": cfg.clarification.model_copy(update={"review": "never"})}
    )
    model = BoundaryModel(2)
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write result", cfg, source)
    assert projection(engine.journal, run)["status"] == "completed"
    assert model.native_calls == model.cleaned == 3
    assert model.commands[0] == model.commands[1] == model.commands[2]
    events = engine.journal.events(run)
    waits = [e["body"] for e in events if e["kind"] == "transport_retry_wait"]
    assert [e["retry"] for e in waits] == [1, 2]
    assert sum(clock[1]) == pytest.approx(15, abs=0.01)
    reserved = [
        e["body"]
        for e in events
        if e["kind"] == "reserved" and e["body"]["stage"] == "clarification"
    ]
    assert [e["call_key"].rsplit(":", 1)[1] for e in reserved] == [
        "output",
        "transport",
        "transport",
    ]
    assert len([e for e in events if e["kind"] == "settled"]) == len(
        [e for e in events if e["kind"] == "reserved"]
    )


@pytest.mark.parametrize("retries", [0, 2])
def test_capacity_exhaustion_and_resume_cannot_reset_allowance(tmp_path, clock, retries):
    model = BoundaryModel(10)
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={"clarification": StagePolicy(model="test-only", transport_retries=retries)}
    )
    with pytest.raises(RuntimeError, match="MODEL_AT_CAPACITY_RETRIES_EXHAUSTED"):
        engine.start("Write result", cfg, source)
    assert model.native_calls == model.cleaned == retries + 1
    run = engine.journal.runs()[0]
    assert projection(engine.journal, run)["status"] == "failed"
    reopened = Engine(Journal(engine.journal.path), engine.candidates, model, engine.root)
    with pytest.raises(RuntimeError, match="MODEL_AT_CAPACITY_RETRIES_EXHAUSTED"):
        reopened.execute(run)
    assert model.native_calls == retries + 1


@pytest.mark.parametrize(
    "output,exception",
    [
        (
            CAPACITY
            + "\n"
            + json.dumps({"type": "turn.failed", "error": {"message": "usage_limit_reached"}}),
            Stopped,
        ),
        (
            json.dumps({"type": "turn.failed", "error": {"message": "Authentication failed"}}),
            RuntimeError,
        ),
        ("unknown process failure", RuntimeError),
    ],
)
def test_other_failures_do_not_retry(tmp_path, clock, output, exception):
    model = BoundaryModel(10, output)
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={"clarification": StagePolicy(model="test-only", transport_retries=2)}
    )
    with pytest.raises(exception):
        engine.start("Write result", cfg, source)
    assert model.native_calls == model.cleaned == 1
    assert not clock[1]


def test_wait_obeys_wall_budget(tmp_path, clock):
    model = BoundaryModel(10)
    engine, source = runtime(tmp_path, model)
    cfg = config(wall_seconds=1).model_copy(
        update={"clarification": StagePolicy(model="test-only", transport_retries=2)}
    )
    with pytest.raises(Stopped, match="WALL_BUDGET_EXHAUSTED"):
        engine.start("Write result", cfg, source)
    assert model.native_calls == model.cleaned == 1


def test_wait_obeys_cancellation(tmp_path, clock, monkeypatch):
    model = BoundaryModel(10)
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={"clarification": StagePolicy(model="test-only", transport_retries=2)}
    )

    def cancel(seconds):
        engine.journal.stop(engine.journal.runs()[0], "OPERATOR_CANCELLED")

    monkeypatch.setattr(time, "sleep", cancel)
    with pytest.raises(Stopped, match="OPERATOR_CANCELLED"):
        engine.start("Write result", cfg, source)
    assert model.native_calls == model.cleaned == 1


def test_uncertain_writes_never_retry(tmp_path, clock):
    model = BoundaryModel(10)
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={"security": "balanced", "authority_ceiling": Authority(external_writes=True)}
    )
    run = engine.prepare("Write result", cfg, source)
    with pytest.raises(Waiting, match="UNCERTAIN_EXTERNAL_EFFECT"):
        engine._generate(
            run,
            "clarification",
            StagePolicy(model="test-only", transport_retries=2),
            {},
            Clarification,
            source,
            Authority(external_writes=True),
        )
    assert model.native_calls == model.cleaned == 1
    assert not clock[1]


def test_restart_during_wait_preserves_failed_call_and_delay(tmp_path, clock, monkeypatch):
    model = BoundaryModel(2)
    engine, source = runtime(tmp_path, model)
    policy = StagePolicy(model="test-only", transport_retries=2)
    cfg = config().model_copy(update={"clarification": policy})
    run = engine.prepare("Write result", cfg, source)
    sleep = time.sleep

    def interrupt(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        engine._generate(run, "clarification", policy, {}, Clarification, source)
    monkeypatch.setattr(time, "sleep", sleep)
    reopened = Engine(Journal(engine.journal.path), engine.candidates, model, engine.root)
    result = reopened._generate(run, "clarification", policy, {}, Clarification, source)
    assert result == clarification()
    assert model.native_calls == model.cleaned == 3
    assert sum(clock[1]) == pytest.approx(15, abs=0.01)
