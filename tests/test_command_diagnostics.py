"""Native fixtures reach durable command inspection without Docker or model access."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from ai_employee.cli import main
from ai_employee.command_diagnostics import CommandCapture, command_event
from ai_employee.container import ContainerModel
from ai_employee.diagnostics import execution_snapshot
from ai_employee.history import Journal, Stopped
from ai_employee.isolated_worker import IsolatedWorkerProfile
from ai_employee.models import RunConfig

from .test_autonomous_runtime import OfflineModel, config, runtime
from .test_stage_contracts import stream

SECRET = "sk-privateFixtureCredential123456789"


def event(item="cmd-1", phase="completed", **fields):
    return {
        "type": "item." + phase,
        "item": {"id": item, "type": "command_execution", **fields},
    }


class CommandModel(OfflineModel):
    """Run the actual ContainerModel observation path with container I/O replaced."""

    def __init__(self, failure=None):
        super().__init__()
        self.failure = failure

    def generate(self, policy, prompt, schema, workspace, authority, timeout, cancelled, **kw):
        result, _ = super().generate(
            policy, prompt, schema, workspace, authority, timeout, cancelled
        )
        profile = IsolatedWorkerProfile(image="sha256:" + "a" * 64, auth_file="/fixture/auth")
        native = ContainerModel(profile)
        candidate = MagicMock(profile=profile, deadline=None, proxy=None)
        candidate.name = "fixture"
        events = [
            event(phase="started", command="tool --token " + SECRET, status="in_progress"),
            event(phase="updated", aggregated_output="partial harmless output"),
        ]
        if not self.failure:
            events += [
                event(exit_code=127, status="failed", aggregated_output="tool not found"),
                event(exit_code=127, status="failed", aggregated_output="tool not found"),
                event("cmd-2", "started", command="echo ok"),
                event("cmd-2", exit_code=0, status="completed", stdout="ok", stderr=""),
            ]
        output = stream(result.model_dump())

        def guarded(*args, **kwargs):
            for e in events:
                kwargs["observe"](e)
            if self.failure == "quota":
                kwargs["observe"]({"type": "error", "message": "usage_limit_reached"})
            if self.failure == "timeout":
                raise TimeoutError("fixture timeout")
            if self.failure == "cancel":
                raise Stopped("CANCELLED")
            if self.failure == "disconnect":
                raise ConnectionError("fixture disconnected")
            for line in output.splitlines():
                kwargs["observe"](json.loads(line))
            return 0, output.encode(), b""

        candidate.run_guarded.side_effect = guarded

        @contextmanager
        def owned(*args, **kwargs):
            yield candidate

        with (
            patch.object(native, "_candidate", owned),
            patch.object(native, "_native_probe"),
            patch.object(native, "_copy_workspace"),
        ):
            return native.generate(
                policy, prompt, schema, workspace, authority, timeout, cancelled, **kw
            )


def enabled(**kwargs):
    return config().model_copy(update={"command_capture": CommandCapture(**kwargs)})


def fixture_journal(tmp_path, **kwargs):
    journal = Journal(tmp_path / "history.db")
    cfg = enabled(**kwargs)
    run = journal.create("fixture", cfg)
    reservation, _ = journal.reserve(run, "worker")
    return journal, run, reservation, cfg.command_capture


def store(journal, run, reservation, policy, raw, elapsed=0):
    observation = command_event(raw, elapsed)
    journal.command_snapshot(
        run, reservation, "worker", observation, policy, {"task": "task-1", "attempt": "attempt-1"}
    )


def test_native_engine_restart_and_cli_filters(tmp_path, capsys):
    engine, source = runtime(tmp_path, CommandModel())
    run = engine.start("Write result", enabled(), source)
    journal = Journal(engine.journal.path)
    records = journal.commands(run)["records"]
    assert len(records) == 10  # Five independent stage sessions, two commands each.
    assert SECRET not in json.dumps(records)
    assert len({r["reservation"] for r in records}) == 5
    worker = journal.commands(run, stage="worker", failed_only=True)["records"]
    assert len(worker) == 1
    r = worker[0]
    assert r["task"] and r["attempt"]
    assert r["command"]["redactions"] > 0 and r["exit_code"] == 127
    assert r["combined_output"]["text"] == "tool not found"
    assert r["stdout"]["status"] == "unavailable"
    assert r["cwd"]["status"] == "unavailable"  # Session cwd is not command cwd.
    assert r["session_workspace"] == "/work"
    assert r["completion_observation"] == "completed_event"
    assert r["observed_duration_seconds"] >= 0 and r["provider_duration_seconds"] is None
    successful = next(r for r in records if r["command_id"]["text"] == "cmd-2")
    assert successful["output_format"] == "separate" and successful["stderr"]["text"] == ""
    assert successful["stderr"]["status"] == "available"
    assert main(["--state", str(tmp_path), "commands", run, "--stage", "worker", "--failed"]) == 0
    view = json.loads(capsys.readouterr().out)["commands"]
    assert view["records"] == worker
    assert main(["--state", str(tmp_path), "commands", run, "--reservation", r["reservation"]]) == 0
    assert len(json.loads(capsys.readouterr().out)["commands"]["records"]) == 2
    engine.execute(run)
    engine.cleanup(run)
    assert journal.commands(run)["records"] == records
    assert not any(
        e["kind"] == "worker_observation"
        and e["body"]["observation"].get("event") == "command_snapshot"
        for e in journal.events(run)
    )


@pytest.mark.parametrize("failure", ["quota", "timeout", "cancel", "disconnect"])
def test_partial_commands_survive_failed_invocations(tmp_path, failure):
    engine, source = runtime(tmp_path, CommandModel(failure))
    run = engine.prepare("Write result", enabled(), source)
    with pytest.raises((Stopped, TimeoutError, ConnectionError, RuntimeError)):
        engine.execute(run)
    rows = Journal(engine.journal.path).commands(run)["records"]
    assert rows
    assert all(r["combined_output"]["text"] == "partial harmless output" for r in rows)
    assert all(r["finished_at"] is None and r["exit_code"] is None for r in rows)
    assert all(
        r["completion_observation"] == "invocation_ended_without_command_completion" for r in rows
    )
    assert engine.journal.budget(run)["open_reservations"] == 0
    if failure == "quota":
        assert len(rows) == 1
        assert any(
            e["kind"] == "stopped" and e["body"]["reason"] == "USAGE_LIMIT"
            for e in engine.journal.events(run)
        )


@pytest.mark.parametrize("failure", [None, "quota"])
def test_logging_failure_does_not_change_success_or_quota(tmp_path, monkeypatch, failure):
    engine, source = runtime(tmp_path, CommandModel(failure))

    def broken(*args, **kwargs):
        raise OSError("secret error text " + SECRET)

    monkeypatch.setattr(engine.journal, "command_snapshot", broken)
    run = engine.prepare("Write result", enabled(), source)
    if failure:
        with pytest.raises(Stopped, match="USAGE_LIMIT"):
            engine.execute(run)
    else:
        engine.execute(run)
    events = engine.journal.events(run)
    assert any(e["kind"] == ("stopped" if failure else "completed") for e in events)
    assert any(e["kind"] == "command_capture_failed" for e in events)
    assert engine.journal.commands(run)["availability"] == "capture_incomplete"
    assert SECRET not in json.dumps(events)
    assert engine.journal.budget(run)["open_reservations"] == 0


def test_duplicates_out_of_order_multiple_commands_and_provider_time(tmp_path):
    j, run, reservation, policy = fixture_journal(tmp_path)
    store(j, run, reservation, policy, event(phase="started", command="first", cwd="/work/sub"), 10)
    store(j, run, reservation, policy, event("second", "started", command="second"), 11)
    store(j, run, reservation, policy, event(phase="started", command="first", cwd="/work/sub"), 12)
    store(
        j,
        run,
        reservation,
        policy,
        event(aggregated_output="done", exit_code=0, duration_ms=123),
        15,
    )
    before = j.commands(run)["records"]
    store(
        j,
        run,
        reservation,
        policy,
        event(aggregated_output="done", exit_code=0, duration_ms=123),
        50,
    )
    store(j, run, reservation, policy, event(phase="updated", aggregated_output="old"), 51)
    assert j.commands(run)["records"] == before
    assert before[0]["observed_duration_seconds"] == 5
    assert before[0]["provider_duration_seconds"] == 0.123
    assert before[0]["cwd"]["text"] == "/work/sub"
    store(j, run, reservation, policy, event("no-start", exit_code=1), 52)
    row = j.commands(run)["records"][-1]
    assert row["started_at"] is None and row["observed_duration_seconds"] is None
    store(j, run, reservation, policy, event(None, command="unmatched"))
    assert j.commands(run)["omission_reasons"]["missing_command_id"] == 1
    reservation2, _ = j.reserve(run, "worker")
    store(j, run, reservation2, policy, event(command="same ID new attempt", exit_code=0))
    assert len(j.commands(run)["records"]) == 4


def test_caps_redaction_and_purge_cannot_resurrect(tmp_path, capsys):
    j, run, reservation, policy = fixture_journal(
        tmp_path, command_bytes=4096, run_bytes=8192, max_commands=3
    )
    for i in range(10):
        store(
            j,
            run,
            reservation,
            policy,
            event(
                str(i),
                command="curl --password secret-password",
                aggregated_output="token=" + SECRET + " " + "x" * 20000,
                exit_code=i,
            ),
        )
    view = j.commands(run)
    assert view["omitted_events"] > 0
    assert all(r["combined_output"]["status"] == "truncated" for r in view["records"])
    assert SECRET not in json.dumps(view) and "secret-password" not in json.dumps(view)
    with j.connect() as db:
        sizes = db.execute(
            "SELECT length(CAST(body AS BLOB)) FROM command_records WHERE run=?", (run,)
        ).fetchall()
    assert sum(r[0] for r in sizes) <= 8192 and all(r[0] <= 4096 for r in sizes)
    assert main(["--state", str(tmp_path), "purge-commands", run]) == 0
    assert json.loads(capsys.readouterr().out)["purged_commands"] > 0
    reopened = Journal(j.path)
    store(reopened, run, reservation, policy, event(command="must not resurrect"))
    view = reopened.commands(run)
    assert view["purged"] and all(r["unavailable"] == "purged" for r in view["records"])
    assert "must not resurrect" not in j.path.read_bytes().decode(errors="ignore")
    assert j.events(run)  # Deletion does not rewrite the hash chain.


def test_retention_and_disabled_capture(tmp_path, monkeypatch):
    j, run, reservation, policy = fixture_journal(tmp_path)
    store(j, run, reservation, policy, event(command="expires"))
    with j.connect() as db:
        db.execute("UPDATE command_records SET expires=0")
    reopened = Journal(j.path)
    assert reopened.commands(run)["records"][0]["unavailable"] == "expired"
    store(reopened, run, reservation, policy, event(command="late replay"))
    assert reopened.commands(run)["records"][0]["unavailable"] == "expired"
    j.command_snapshot(
        run,
        reservation,
        "worker",
        command_event(event("off", command="disabled body"), 0),
        CommandCapture(enabled=False),
        {},
    )
    assert "disabled body" not in j.path.read_bytes().decode(errors="ignore")


def test_old_configuration_digest_and_history_without_tables_remain_readable(tmp_path):
    cfg = config()
    old_payload = cfg.model_dump(mode="json", exclude={"command_capture", "direct_execution"})
    old_text = json.dumps(old_payload, sort_keys=True, separators=(",", ":"))
    assert cfg.digest == hashlib.sha256(old_text.encode()).hexdigest()
    assert RunConfig.model_validate_json(old_text).canonical() == old_text
    j = Journal(tmp_path / "old.db")
    run = j.create("old", cfg)
    with sqlite3.connect(j.path) as db:
        db.execute("DROP TABLE command_records")
        db.execute("DROP TABLE command_capture_state")
    j = Journal(j.path)
    assert j.config(run).digest == cfg.digest
    assert j.commands(run)["availability"] == "disabled_or_legacy"
    assert not j.commands(run)["records"]


def test_failure_streams_do_not_retain_command_or_reasoning_bodies():
    raw = "\n".join(
        json.dumps(e)
        for e in [
            event(command="sensitive command", aggregated_output="tool content"),
            {"type": "item.completed", "item": {"type": "reasoning", "text": "private reasoning"}},
            {"type": "error", "message": "provider failure", "token": SECRET},
        ]
    )
    value = execution_snapshot(raw.encode(), raw.encode(), started=True)
    serialized = json.dumps(value)
    assert all(
        s not in serialized
        for s in ["sensitive command", "tool content", "private reasoning", SECRET]
    )
    assert "provider failure" in serialized


def test_init_enables_capture_and_can_disable_it(tmp_path, capsys):
    for disabled in (False, True):
        p = tmp_path / f"config-{disabled}.json"
        args = [
            "init",
            "--model",
            "fixture",
            "--image",
            "sha256:" + "a" * 64,
            "--auth-file",
            str(tmp_path / "auth"),
            "--output",
            str(p),
        ]
        if disabled:
            args.append("--no-command-capture")
        assert main(args) == 0
        assert json.loads(p.read_text())["command_capture"]["enabled"] is not disabled
    capsys.readouterr()


@pytest.mark.parametrize("failure", ["timeout", "cancel", "quota", "nonzero"])
def test_actual_pipe_observations_survive_stopping_and_cleanup(tmp_path, monkeypatch, failure):
    from .test_execution_diagnostics import pipe_runtime

    engine, run, processes, _ = pipe_runtime(
        tmp_path,
        monkeypatch,
        failure,
        command_events=[
            event(phase="started", command="missing-tool"),
            event(phase="updated", aggregated_output="partial before stop"),
        ],
        capture=CommandCapture(),
    )
    with pytest.raises((RuntimeError, Stopped)):
        engine.execute(run)
    rows = Journal(engine.journal.path).commands(run)["records"]
    assert rows and rows[0]["command"]["text"] == "missing-tool"
    assert rows[0]["combined_output"]["text"] == "partial before stop"
    assert rows[0]["completion_observation"] == "invocation_ended_without_command_completion"
    assert all(p.poll() is not None for p in processes)
    assert engine.journal.budget(run)["open_reservations"] == 0


def test_concurrent_capture_respects_shared_run_budget(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    j, run, reservation, policy = fixture_journal(tmp_path, command_bytes=4096, run_bytes=8192)

    def write(i):
        store(
            j, run, reservation, policy, event(str(i), command="cmd", aggregated_output="x" * 20000)
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(30)))
    with j.connect() as db:
        size = db.execute(
            "SELECT SUM(length(CAST(body AS BLOB))) FROM command_records WHERE run=?", (run,)
        ).fetchone()[0]
    assert size <= policy.run_bytes
    assert j.commands(run)["omission_reasons"]["byte_limit"] > 0


@pytest.mark.parametrize(
    "text,secret",
    [
        ("tool --api-key abc-private", "abc-private"),
        ("https://operator:password-value@example.test", "password-value"),
        ("github_pat_privatefixturecredential123", "github_pat_privatefixturecredential123"),
        ('{"refresh_token": "quoted secret"}', "quoted secret"),
        ("-----BEGIN PRIVATE KEY-----\nprivate-partial", "private-partial"),
    ],
)
def test_known_secrets_are_removed_before_storage_and_truncation(tmp_path, text, secret):
    j, run, reservation, policy = fixture_journal(tmp_path, command_bytes=4096)
    store(
        j,
        run,
        reservation,
        policy,
        event(SECRET, command=text, aggregated_output=text + "x" * 10000),
    )
    row = j.commands(run)["records"][0]
    assert row["command"]["redactions"] > 0
    assert row["command_id"]["redactions"] > 0
    assert secret not in json.dumps(row) and SECRET not in json.dumps(row)
    assert secret.encode() not in j.path.read_bytes()


def test_complete_native_event_without_final_newline_is_observed(tmp_path, monkeypatch):
    from .test_execution_diagnostics import pipe_runtime

    engine, run, _, _ = pipe_runtime(
        tmp_path,
        monkeypatch,
        "guard",
        capture=CommandCapture(),
        final_newline=False,
        command_events=[event(command="no-final-newline", exit_code=1)],
    )
    with pytest.raises(RuntimeError):
        engine.execute(run)
    row = engine.journal.commands(run)["records"][0]
    assert row["command"]["text"] == "no-final-newline" and row["exit_code"] == 1


def test_no_command_body_or_model_reasoning_is_captured_when_disabled(tmp_path):
    engine, source = runtime(tmp_path, CommandModel())
    run = engine.start("Write result", enabled(enabled=False), source)
    assert engine.journal.commands(run)["records"] == []
    with engine.journal.connect() as db:
        assert db.execute("SELECT count(*) FROM command_records").fetchone()[0] == 0
    assert b"partial harmless output" not in engine.journal.path.read_bytes()


def test_invalid_capture_budget_is_rejected():
    with pytest.raises(ValueError, match="COMMAND_CAPTURE_EXCEEDS_RUN_CAP"):
        CommandCapture(command_bytes=8192, run_bytes=4096)


def test_context_is_allowlisted_and_redacted_without_overriding_runtime_identity(tmp_path):
    j, run, reservation, policy = fixture_journal(tmp_path)
    j.command_snapshot(
        run,
        reservation,
        "worker",
        command_event(event(command="ok"), 0),
        policy,
        {"task": SECRET, "attempt": "attempt-1", "run": "forged", "extra": SECRET},
    )
    row = j.commands(run)["records"][0]
    assert row["run"] == run and row["task"] == "[REDACTED]"
    assert row["context_redactions"] == 1 and "extra" not in row
    assert SECRET.encode() not in j.path.read_bytes()
