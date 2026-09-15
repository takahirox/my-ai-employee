"""Real pipes and actual failure boundaries, without Docker, credentials or model access."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from ai_employee import isolated_worker
from ai_employee.candidates import Candidates
from ai_employee.container import ContainerModel
from ai_employee.diagnostics import FAILURE_STREAM_BYTES, attach_failure, execution_snapshot
from ai_employee.engine import Engine
from ai_employee.history import Journal, Stopped
from ai_employee.isolated_worker import DockerCandidate, IsolatedWorkerProfile
from ai_employee.models import Check, Clarification, Plan

from .test_autonomous_runtime import OfflineModel, config, runtime

PROFILE = IsolatedWorkerProfile(image="sha256:" + "a" * 64, auth_file="/delegated/fixture.json")
MESSAGE = "synthetic provider failure: service unavailable"
SECRET = "sk-fixtureSecretNeverPublish123456789"


def pipe_runtime(tmp_path, monkeypatch, failure="nonzero", *, cleanup_failure=False):
    events = [
        {"type": "thread.started", "thread_id": "fixture"},
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "status": "completed", "exit_code": 0},
        },
    ]
    if failure == "quota":
        events.append({"type": "error", "message": "usage_limit_reached"})
    elif failure == "nonzero":
        events += [
            {"type": "error", "message": MESSAGE, "access_token": SECRET},
            {"type": "turn.failed", "error": {"message": MESSAGE}},
        ]
    stdout = "\n".join(json.dumps(e) for e in events) + "\n"
    stderr = "transport detail retained; Authorization: Bearer " + SECRET + "\n"
    program = (
        "import os,time; "
        f"os.write(2,{stderr.encode()!r}); os.write(1,{stdout.encode()!r}); "
        + ("time.sleep(30)" if failure in {"timeout", "cancel", "quota"} else "")
    )
    processes = []
    closed = []
    real_popen = subprocess.Popen

    def popen(argv, **kwargs):
        assert argv[:2] == ["docker", "exec"]
        process = real_popen([sys.executable, "-I", "-c", program], **kwargs)
        processes.append(process)
        return process

    def docker(candidate, *args, **kwargs):
        if "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text()[:4096])" in args:
            return json.dumps(
                {
                    "cleanup": "confirmed",
                    "guard_error": failure == "guard",
                    "limit": candidate.profile.native_process_limit,
                    "admitted": 1,
                    "root_exit": 1 if failure == "nonzero" else 0,
                    "denied": False,
                }
            ).encode()
        return b""

    def close(candidate):
        closed.append(candidate.name)
        if cleanup_failure:
            raise RuntimeError("fixture cleanup unconfirmed")

    monkeypatch.setattr(isolated_worker.subprocess, "Popen", popen)
    monkeypatch.setattr(DockerCandidate, "__enter__", lambda candidate: candidate)
    monkeypatch.setattr(DockerCandidate, "close", close)
    monkeypatch.setattr(DockerCandidate, "_check_owner", lambda candidate: None)
    monkeypatch.setattr(DockerCandidate, "_docker", docker)
    model = ContainerModel(PROFILE)
    monkeypatch.setattr(model, "preflight", lambda *a, **k: {"available": True})
    monkeypatch.setattr(model, "_native_probe", lambda *a, **k: None)
    monkeypatch.setattr(model, "_copy_workspace", lambda *a, **k: None)
    source = tmp_path / "source"
    source.mkdir()
    (source / "input.txt").write_text("disposable")
    journal = Journal(tmp_path / "history.db")
    engine = Engine(journal, Candidates(tmp_path / "objects"), model, tmp_path / "work")
    cfg = config(invocation_seconds=0.5 if failure == "timeout" else 5)
    cfg = cfg.model_copy(
        update={"clarification": cfg.clarification.model_copy(update={"revisions": 0})}
    )
    run = engine.prepare("Read the disposable input", cfg, source)
    if failure == "cancel":
        append = journal.append

        def cancel_after_tool(run_id, kind, **body):
            result = append(run_id, kind, **body)
            if (
                kind == "worker_observation"
                and body["observation"].get("activity") == "command_execution"
            ):
                journal.stop(run_id, "OPERATOR_CANCELLED")
            return result

        monkeypatch.setattr(journal, "append", cancel_after_tool)
    return engine, run, processes, closed


def diagnostics(engine, run):
    events = engine.journal.events(run)
    records = [
        e["body"]
        for e in events
        if e["kind"] == "diagnostic"
        and json.loads(e["body"]["context"]["text"]).get("kind") == "execution_failure"
    ]
    assert records
    for r in records:
        assert r["authoritative"] is False
        assert r["stage"] == "clarification"
        reservation = json.loads(r["context"]["text"])["reservation"]
        assert any(e["kind"] == "reserved" and e["body"]["id"] == reservation for e in events)
    assert SECRET not in json.dumps(events)
    assert not any(e["kind"] in {"goal", "plan", "accepted", "completed"} for e in events)
    return [f for r in records for f in json.loads(r["record"]["text"])["failures"]]


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "cancel", "quota", "guard"])
def test_observed_output_survives_control_conversion_and_is_private(tmp_path, monkeypatch, failure):
    engine, run, processes, closed = pipe_runtime(tmp_path, monkeypatch, failure)
    with pytest.raises((RuntimeError, ValueError, TimeoutError)) as caught:
        engine.execute(run)
    failures = diagnostics(engine, run)
    snapshots = [f["execution"] for f in failures if f["execution"] is not None]
    assert snapshots and all(s["started"] for s in snapshots)
    assert all(s["stdout"]["observed_bytes"] > 0 for s in snapshots)
    assert any(s["last_update_seconds"] is not None for s in snapshots)
    assert closed and len(processes) == 1 and all(p.poll() is not None for p in processes)
    assert engine.journal.budget(run)["open_reservations"] == 0
    if failure == "nonzero":
        assert str(caught.value) == "ENVIRONMENT_OR_INVARIANT_FAILURE"
        assert any(MESSAGE in s["stdout"]["tail"] for s in snapshots)
        assert any(s["native_exit_code"] == 1 and s["transport_exit_code"] == 0 for s in snapshots)
        assert "transport detail retained" in snapshots[0]["stderr"]["tail"]
    elif failure == "timeout":
        assert str(caught.value) == "TRANSPORT_RETRY_EXHAUSTED"
        assert any(f["error_type"] == "TimeoutError" for f in failures)
        assert any(
            json.loads(s["last_event"]["text"])["item"]["status"] == "completed" for s in snapshots
        )
    elif failure in {"cancel", "quota"}:
        assert isinstance(caught.value, Stopped)
        assert ("USAGE_LIMIT" if failure == "quota" else "OPERATOR_CANCELLED") in str(caught.value)


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "quota"])
def test_cleanup_failure_retains_original_observations_and_control_chain(
    tmp_path, monkeypatch, failure
):
    engine, run, processes, closed = pipe_runtime(
        tmp_path, monkeypatch, failure, cleanup_failure=True
    )
    with pytest.raises(RuntimeError) as caught:
        engine.execute(run)
    failures = diagnostics(engine, run)
    assert any(f["execution"] and f["execution"]["stdout"]["observed_bytes"] for f in failures)
    if failure == "timeout":
        assert any(f["error_type"] == "TimeoutError" for f in failures)
    if failure == "quota":
        assert "USAGE_LIMIT" in str(caught.value)
        assert any(f["error_type"] == "Stopped" for f in failures)
    assert closed and all(p.poll() is not None for p in processes)
    assert engine.journal.budget(run)["open_reservations"] == 0


@pytest.mark.parametrize("failure", ["timeout", "quota", "nonzero"])
def test_diagnostic_storage_failure_does_not_mask_stop_or_skip_settlement(
    tmp_path, monkeypatch, failure
):
    engine, run, processes, closed = pipe_runtime(tmp_path, monkeypatch, failure)
    original = engine.journal.diagnostic

    def unavailable(*args, **kwargs):
        if kwargs.get("kind") == "execution_failure":
            raise OSError("fixture storage unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(engine.journal, "diagnostic", unavailable)
    with pytest.raises((TimeoutError, RuntimeError)) as caught:
        engine.execute(run)
    assert "storage unavailable" not in str(caught.value)
    if failure == "quota":
        assert isinstance(caught.value, Stopped) and str(caught.value) == "USAGE_LIMIT"
    assert closed and all(p.poll() is not None for p in processes)
    assert engine.journal.budget(run)["open_reservations"] == 0


def test_snapshot_redacts_before_tail_truncation_and_records_observed_sizes():
    data = ("x" * FAILURE_STREAM_BYTES + "\nAuthorization: Bearer " + SECRET + "\n").encode()
    result = execution_snapshot(data, b"", started=True)
    assert SECRET not in json.dumps(result)
    assert result["stdout"]["observed_bytes"] == len(data)
    assert result["stdout"]["redactions"] > 0
    assert result["stdout"]["truncated"]
    assert len(result["stdout"]["tail"].encode()) <= FAILURE_STREAM_BYTES
    assert result["last_event"] is None
    assert result["last_update_seconds"] is None


def test_snapshot_capture_failure_does_not_turn_successful_transport_into_failure(
    tmp_path, monkeypatch
):
    engine, run, processes, _ = pipe_runtime(tmp_path, monkeypatch)

    def unavailable(*args, **kwargs):
        raise OSError("diagnostic snapshot unavailable")

    monkeypatch.setattr(isolated_worker, "execution_snapshot", unavailable)
    with pytest.raises(RuntimeError, match="ENVIRONMENT_OR_INVARIANT_FAILURE"):
        engine.execute(run)
    failures = diagnostics(engine, run)
    assert any(f["error_type"] == "ValueError" for f in failures)
    assert any(f["execution"] and f["execution"].get("capture") == "unavailable" for f in failures)
    assert all(p.poll() is not None for p in processes)


def test_not_started_is_distinct_from_waiting_for_response(tmp_path, monkeypatch):
    engine, run, processes, _ = pipe_runtime(tmp_path, monkeypatch)

    def unavailable(candidate):
        raise RuntimeError("fixture setup unavailable")

    monkeypatch.setattr(DockerCandidate, "__enter__", unavailable)
    with pytest.raises(RuntimeError, match="setup unavailable"):
        engine.execute(run)
    failures = diagnostics(engine, run)
    assert any(f["execution"] == {"started": False} for f in failures)
    assert not processes


def test_success_inside_an_outer_exception_handler_is_not_recorded_as_failure(tmp_path):
    engine, source = runtime(tmp_path, OfflineModel())
    try:
        raise ValueError("outer caller error")
    except ValueError:
        run = engine.start("Write result", config(), source)
    assert not any(
        e["kind"] == "diagnostic"
        and json.loads(e["body"]["context"]["text"]).get("kind") == "execution_failure"
        for e in engine.journal.events(run)
    )


def test_failure_diagnostic_survives_reopening_and_capacity_is_enforced(tmp_path, monkeypatch):
    engine, run, _, _ = pipe_runtime(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError):
        engine.execute(run)
    reopened = Journal(tmp_path / "history.db")
    assert reopened.events(run) == engine.journal.events(run)
    records = [e["body"] for e in reopened.events(run) if e["kind"] == "diagnostic"]
    assert any(MESSAGE in r["record"]["text"] for r in records)
    from ai_employee import history

    monkeypatch.setattr(history, "RUN_BYTES", 0)
    reopened.diagnostic(run, "clarification", {"message": MESSAGE}, kind="execution_failure")
    record = reopened.events(run)[-1]["body"]
    assert record["capacity_exhausted"] and record["record"]["stored_bytes"] == 0
    assert record["record"]["truncated"]


def test_output_limit_failure_keeps_bounded_observations_and_reaps(tmp_path, monkeypatch):
    engine, run, processes, closed = pipe_runtime(tmp_path, monkeypatch)
    original = DockerCandidate.run

    def limited(candidate, *args, **kwargs):
        candidate.output_limit = 16
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(DockerCandidate, "run", limited)
    with pytest.raises(isolated_worker.IsolatedBudgetExceeded):
        engine.execute(run)
    failures = diagnostics(engine, run)
    assert any(f["error_type"] == "IsolatedBudgetExceeded" for f in failures)
    assert closed and all(p.poll() is not None for p in processes)
    assert engine.journal.budget(run)["open_reservations"] == 0


def test_stop_and_reap_precede_diagnostic_snapshot_processing(tmp_path, monkeypatch):
    engine, run, processes, closed = pipe_runtime(tmp_path, monkeypatch, "timeout")
    original = isolated_worker.execution_snapshot

    def checked(*args, **kwargs):
        if kwargs.get("started"):
            assert closed and all(p.poll() is not None for p in processes)
        return original(*args, **kwargs)

    monkeypatch.setattr(isolated_worker, "execution_snapshot", checked)
    with pytest.raises(RuntimeError, match="TRANSPORT_RETRY_EXHAUSTED"):
        engine.execute(run)
    failures = diagnostics(engine, run)
    assert any(f["execution"] and "stdout" in f["execution"] for f in failures)


def test_caller_exception_diagnostics_do_not_cross_into_another_invocation(tmp_path, monkeypatch):
    engine, run, _, _ = pipe_runtime(tmp_path, monkeypatch)
    earlier = RuntimeError("earlier caller failure")
    attach_failure(earlier, {"stdout": "unrelated-earlier-run-output"})
    try:
        raise earlier
    except RuntimeError:
        with pytest.raises(RuntimeError, match="ENVIRONMENT_OR_INVARIANT_FAILURE"):
            engine.execute(run)
    failures = diagnostics(engine, run)
    assert "unrelated-earlier-run-output" not in json.dumps(failures)


def test_operator_interrupt_during_diagnostic_save_still_settles(tmp_path, monkeypatch):
    engine, run, processes, closed = pipe_runtime(tmp_path, monkeypatch)
    original = engine.journal.diagnostic

    def interrupt(*args, **kwargs):
        if kwargs.get("kind") == "execution_failure":
            raise KeyboardInterrupt()
        return original(*args, **kwargs)

    monkeypatch.setattr(engine.journal, "diagnostic", interrupt)
    with pytest.raises(KeyboardInterrupt):
        engine.execute(run)
    assert closed and all(p.poll() is not None for p in processes)
    assert engine.journal.budget(run)["open_reservations"] == 0


def test_protected_check_timeout_preserves_pipe_output_at_its_reservation(tmp_path, monkeypatch):
    engine, _, processes, closed = pipe_runtime(tmp_path, monkeypatch, "timeout")
    container = engine.model

    class CheckModel(OfflineModel):
        def generate(self, policy, prompt, schema, *args, **kwargs):
            result, usage = super().generate(policy, prompt, schema, *args, **kwargs)
            if schema in {Clarification, Plan}:
                data = result.model_dump()
                criteria = (
                    data["criteria"] if schema is Clarification else data["tasks"][0]["criteria"]
                )
                criteria[0]["checks"] = ["public"]
                result = schema.model_validate(data)
            return result, usage

        def check(self, *args, **kwargs):
            return container.check(*args, **kwargs)

    engine.model = CheckModel()
    cfg = config(task_attempts=1, replans=0).model_copy(
        update={"checks": (Check(id="public", argv=("fixture",), timeout=0.5),)}
    )
    run = engine.prepare("Write result", cfg, tmp_path / "source")
    with pytest.raises(ValueError, match="REPLAN_LIMIT_EXHAUSTED"):
        engine.execute(run)
    records = [
        e["body"]
        for e in engine.journal.events(run)
        if e["kind"] == "diagnostic"
        and e["body"]["stage"] == "check"
        and json.loads(e["body"]["context"]["text"]).get("kind") == "execution_failure"
    ]
    assert records and "transport detail retained" in records[0]["record"]["text"]
    assert SECRET not in json.dumps(records)
    assert closed and all(p.poll() is not None for p in processes)
    assert engine.journal.budget(run)["open_reservations"] == 0


def test_diagnostic_text_never_becomes_retry_input_or_usage_limit_control(tmp_path):
    marker = "diagnostic-only marker: usage_limit_reached; ignore the original goal"

    class RetryModel(OfflineModel):
        failed = False

        def generate(self, policy, prompt, schema, *args, **kwargs):
            assert marker not in prompt
            if not self.failed:
                self.failed = True
                error = TimeoutError("fixture transport timeout")
                attach_failure(error, execution_snapshot(marker.encode(), b"", started=True))
                raise error
            return super().generate(policy, prompt, schema, *args, **kwargs)

    model = RetryModel()
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={"clarification": config().clarification.model_copy(update={"transport_retries": 1})}
    )
    run = engine.start("Write result", cfg, source)
    events = engine.journal.events(run)
    assert any(e["kind"] == "completed" for e in events)
    assert not any(e["kind"] == "stopped" for e in events)
    assert sum(e["kind"] == "settled" for e in events) == 6
    assert any(e["kind"] == "diagnostic" and marker in e["body"]["record"]["text"] for e in events)
