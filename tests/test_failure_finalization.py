"""Termination facts precede disposal, retain identity, and never authorize acceptance."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_employee.container import ContainerModel
from ai_employee.diagnostics import failure_snapshots
from ai_employee.history import Journal
from ai_employee.isolated_worker import DockerCandidate
from ai_employee.stage_contracts import OutputViolation

from .test_execution_diagnostics import PROFILE, diagnostics, pipe_runtime


def lifecycle(monkeypatch, events, *, cleanup_failure=False):
    def enter(candidate):
        candidate.created = True
        candidate.disposal = "pending"
        events.append("enter")
        return candidate

    def close(candidate):
        events.append("dispose")
        candidate.disposal = "unconfirmed" if cleanup_failure else "confirmed"
        if cleanup_failure:
            raise RuntimeError("fixture disposal failed")
        candidate.created = False

    monkeypatch.setattr(DockerCandidate, "__enter__", enter)
    monkeypatch.setattr(DockerCandidate, "close", close)


@pytest.mark.parametrize(
    "error", [ValueError("WORKER_PROCESS_FAILED"), OutputViolation("INVALID_STRUCTURED_OUTPUT")]
)
@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_eligible_failure_is_recorded_before_disposal(
    tmp_path, monkeypatch, error, cleanup_failure
):
    events = []
    lifecycle(monkeypatch, events, cleanup_failure=cleanup_failure)
    model = ContainerModel(PROFILE)
    record = DockerCandidate.record_termination

    def record_termination(candidate, failure):
        assert candidate.created
        events.append("record")
        record(candidate, failure)

    monkeypatch.setattr(DockerCandidate, "record_termination", record_termination)
    with (
        pytest.raises((ValueError, RuntimeError)) as caught,
        model._candidate(tmp_path, 30, lambda: False, models=False) as candidate,
    ):
        candidate.begin_execution("model")
        candidate.native_completion = {"root_exit": 1, "cleanup": "confirmed"}
        raise error
    assert events == ["enter", "record", "dispose"]
    termination = candidate.execution_diagnostic["termination"]
    assert termination["retention"] == {"eligible": True, "reason": "eligible"}
    assert termination["process_stop"] == "confirmed"
    assert termination["reason"] == (
        "native_nonzero"
        if isinstance(error, ValueError) and not isinstance(error, OutputViolation)
        else "response_invalid"
    )
    assert termination["disposal"] == ("unconfirmed" if cleanup_failure else "confirmed")
    chain = failure_snapshots(caught.value)["failures"]
    assert any(item["error_type"] == type(error).__name__ for item in chain)


@pytest.mark.parametrize(
    "state,reason",
    [
        ("unknown", "stop_unconfirmed"),
        ("destroyed", "environment_unavailable"),
        ("cancelled", "cancelled"),
        ("deadline", "deadline_exhausted"),
        ("owner", "control_unavailable"),
    ],
)
def test_retention_requires_all_runtime_preconditions(tmp_path, monkeypatch, state, reason):
    events = []
    lifecycle(monkeypatch, events)
    model = ContainerModel(PROFILE)
    with (
        pytest.raises(RuntimeError),
        model._candidate(tmp_path, 30, lambda: state == "cancelled", models=False) as candidate,
    ):
        candidate.begin_execution("model")
        if state != "unknown":
            candidate.native_completion = {"root_exit": 1, "cleanup": "confirmed"}
        if state == "destroyed":
            candidate.created = False
        if state == "deadline":
            candidate.deadline = time.monotonic() - 1
        if state == "owner":
            monkeypatch.setattr(
                candidate, "_check_owner", lambda: (_ for _ in ()).throw(RuntimeError("lost"))
            )
        raise RuntimeError("fixture")
    assert candidate.execution_diagnostic["termination"]["retention"] == {
        "eligible": False,
        "reason": reason,
    }
    assert events[-1] == "dispose"


def test_new_launch_cannot_reuse_probe_completion_even_if_setup_fails(tmp_path, monkeypatch):
    lifecycle(monkeypatch, [])
    model = ContainerModel(PROFILE)
    with (
        pytest.raises(OSError),
        model._candidate(tmp_path, 30, lambda: False, models=False) as candidate,
    ):
        candidate.begin_execution("probe")
        candidate.native_completion = {"root_exit": 0, "cleanup": "confirmed"}
        candidate.execution_diagnostic["native_exit_code"] = 0
        monkeypatch.setattr(
            candidate, "_docker", lambda *a, **k: (_ for _ in ()).throw(OSError("fixture"))
        )
        candidate.run_guarded(("fixture",), phase="model")
    termination = candidate.execution_diagnostic["termination"]
    assert termination["phase"] == "model"
    assert termination["process_stop"] == "unknown"
    assert "native_exit_code" not in candidate.execution_diagnostic


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "cancel", "quota", "guard"])
def test_failure_termination_survives_journal_reopen(tmp_path, monkeypatch, failure):
    engine, run, _, _ = pipe_runtime(tmp_path, monkeypatch, failure)
    with pytest.raises((RuntimeError, ValueError, TimeoutError)):
        engine.execute(run)
    engine.journal = Journal(engine.journal.path)
    facts = [f["execution"]["termination"] for f in diagnostics(engine, run) if f["execution"]]
    assert facts and all(f["phase"] == "model" for f in facts)
    assert all(f["retention"]["eligible"] is False for f in facts)
    assert all(
        f["process_stop"] == ("confirmed" if failure == "nonzero" else "unknown") for f in facts
    )
    records = [
        e["body"]
        for e in engine.journal.events(run)
        if e["kind"] == "diagnostic" and e["body"]["context"]
    ]
    contexts = [json.loads(r["context"]["text"]) for r in records]
    assert any(
        c.get("commands", {}).get("capture_enabled") is False
        and c["commands"]["reservation"] == c["reservation"]
        for c in contexts
    )


def test_parallel_failures_have_separate_environment_and_termination_facts(tmp_path, monkeypatch):
    lifecycle(monkeypatch, [])
    model = ContainerModel(PROFILE)

    def execute(index):
        try:
            with model._candidate(
                tmp_path / str(index), 30, lambda: False, models=False
            ) as candidate:
                candidate.begin_execution("probe" if index == 0 else "model")
                if index:
                    candidate.native_completion = {"root_exit": 1, "cleanup": "confirmed"}
                raise ValueError("WORKER_PROCESS_FAILED")
        except ValueError as error:
            return error.fleet_execution_diagnostic["termination"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(execute, [0, 1]))
    assert first["environment"] != second["environment"]
    assert first["process_stop"] == "unknown"
    assert second["process_stop"] == "confirmed"


def test_engine_validation_failure_keeps_returned_native_termination(tmp_path):
    from ai_employee.models import Clarification

    from .test_autonomous_runtime import OfflineModel, config, runtime

    class InvalidResponse(OfflineModel):
        def generate(self, *args, **kwargs):
            result, usage = super().generate(*args, **kwargs)
            if args[2] is Clarification:
                kwargs["observation"](
                    {
                        "event": "execution_termination",
                        "snapshot": {
                            "started": True,
                            "native_exit_code": 0,
                            "termination": {
                                "phase": "model",
                                "reason": "completed",
                                "process_stop": "confirmed",
                                "disposal": "confirmed",
                            },
                        },
                    }
                )
                result = result.model_copy(update={"criteria": result.criteria * 2})
            return result, usage

    engine, source = runtime(tmp_path, InvalidResponse())
    run = engine.prepare("Write result", config(), source)
    with pytest.raises(RuntimeError, match="OUTPUT_REPAIR_EXHAUSTED"):
        engine.execute(run)
    events = engine.journal.events(run)
    snapshots = []
    for event in events:
        if event["kind"] == "diagnostic":
            body = json.loads(event["body"]["record"]["text"])
            if isinstance(body, dict):
                snapshots.extend(f["execution"] for f in body.get("failures", []) if f["execution"])
    assert snapshots
    assert all(s["termination"]["reason"] == "post_execution_failure" for s in snapshots)
    assert all(s["termination"]["disposal"] == "confirmed" for s in snapshots)
    assert not any(
        e["kind"] == "worker_observation"
        and e["body"]["observation"].get("event") == "execution_termination"
        for e in events
    )


def test_successful_execution_with_failed_disposal_is_not_reported_completed(tmp_path, monkeypatch):
    lifecycle(monkeypatch, [], cleanup_failure=True)
    with (
        pytest.raises(RuntimeError, match="fixture disposal failed") as caught,
        ContainerModel(PROFILE)._candidate(tmp_path, 30, lambda: False, models=False) as candidate,
    ):
        candidate.begin_execution("model")
        candidate.native_completion = {"root_exit": 0, "cleanup": "confirmed"}
        candidate.record_termination(None)
    termination = caught.value.fleet_execution_diagnostic["termination"]
    assert termination["reason"] == "cleanup_failure"
    assert termination["disposal"] == "unconfirmed"
    assert termination["process_stop"] == "confirmed"
