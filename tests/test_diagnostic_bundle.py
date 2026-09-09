import json
import sqlite3
import subprocess
import sys
import time

import pytest

from ai_employee import diagnostic_bundle as diagnostics
from ai_employee.storage import SQLiteStore


def insert(database, kind, record, *, index_run=None):
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO records(kind,record_id,run_id,revision,payload) VALUES(?,?,?,?,?)",
            (kind, record["id"], index_run or record["run_id"], 1, json.dumps(record)),
        )


def fixture_db(tmp_path):
    database = tmp_path / "trial.db"
    with SQLiteStore(database):
        pass
    insert(
        database,
        "worker_request_v2",
        {
            "id": "request",
            "run_id": "child",
            "graph_run_id": "parent",
            "node_id": "node",
            "created_at": "2026-01-01T00:00:00+00:00",
            "attempt": 0,
            "content_digest": "a" * 64,
            "goal": "SECRET-CANARY",
        },
        index_run="parent",
    )
    return database


def test_bundle_scopes_children_preserves_causality_and_omits_secrets(tmp_path):
    database = fixture_db(tmp_path)
    for ident, run, time_value, code in [
        ("z-first", "child", "01", "WORKER_PROTOCOL_ERROR"),
        ("a-last", "child", "02", "REPAIR_BUDGET_EXHAUSTED"),
        ("unrelated", "foreign", "00", "CANCELLED"),
    ]:
        insert(
            database,
            "worker_boundary_diagnostic_v2",
            {
                "id": ident,
                "run_id": run,
                "created_at": f"2026-01-01T00:00:{time_value}+00:00",
                "code": code,
                "worker_request_digest": "a" * 64,
                "attempt": int(time_value),
                "exception_message": "SECRET-CANARY",
                "source": {"token": "SECRET-CANARY"},
                "SECRET-CANARY_id": "SECRET-CANARY",
                "status": "SECRET-CANARY",
            },
        )
    logs = tmp_path / "logs"
    logs.mkdir()
    with diagnostics.DiagnosticCollector(database, logs, "parent", interval=60):
        pass
    database.unlink()
    saved = (logs / "fleet-diagnostic-bundle.json").read_text()
    bundle = json.loads(saved)
    assert "SECRET-CANARY" not in saved
    assert diagnostics.reference("foreign") not in saved
    assert bundle["state"] == "controller_finished"
    records = bundle["records"]
    assert [item["record"]["id"] for item in records] == [
        diagnostics.reference(value) for value in ("request", "z-first", "a-last")
    ]
    assert records[1]["record"]["worker_request_digest"] == records[0]["record"]["content_digest"]
    assert records[1]["storage_sequence"] < records[2]["storage_sequence"]
    assert records[1]["record"]["code"] == "WORKER_PROTOCOL_ERROR"
    assert records[2]["record"]["code"] == "REPAIR_BUDGET_EXHAUSTED"
    assert not bundle["truncated"]


@pytest.mark.parametrize(
    "code",
    [
        "TIMEOUT",
        "CANCELLED",
        "NODE_EXECUTION_FAILED",
        "GRAPH_CANCELLED",
        "RUN_WALL_BUDGET_EXCEEDED",
    ],
)
def test_bundle_retains_observed_termination_codes(tmp_path, code):
    database = fixture_db(tmp_path)
    insert(database, "work_run_v2", {"id": "child", "run_id": "child", "failure_code": code})
    bundle = diagnostics.snapshot(database, "parent")
    record = next(item["record"] for item in bundle["records"] if item["kind"] == "work_run_v2")
    assert record["failure_code"] == code


def test_collector_preserves_committed_evidence_after_process_is_killed(tmp_path):
    database = fixture_db(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    script = (
        "import sys,time; from pathlib import Path; "
        "from ai_employee.diagnostic_bundle import DiagnosticCollector; "
        "collector=DiagnosticCollector(Path(sys.argv[1]),Path(sys.argv[2]),'parent',interval=.02); "
        "collector.__enter__(); time.sleep(30)"
    )
    process = subprocess.Popen([sys.executable, "-c", script, str(database), str(logs)])
    try:
        output = logs / "fleet-diagnostic-bundle.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if output.exists() and json.loads(output.read_text())["records"]:
                break
            time.sleep(0.02)
        else:
            pytest.fail("collector did not publish before termination")
        insert(
            database,
            "worker_boundary_diagnostic_v2",
            {"id": "late", "run_id": "child", "code": "WORKER_PROTOCOL_ERROR"},
        )
        while time.monotonic() < deadline:
            if len(json.loads(output.read_text())["records"]) == 2:
                break
            time.sleep(0.02)
        else:
            pytest.fail("collector did not publish the newly committed diagnostic")
        process.kill()
        process.wait(timeout=5)
        database.unlink()
        bundle = json.loads(output.read_text())
        assert bundle["state"] == "running"  # No finalizer: this is explicitly a partial snapshot.
        assert len(bundle["records"]) == 2
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_export_failure_retains_last_snapshot_and_reports_safe_error(tmp_path, monkeypatch):
    database = fixture_db(tmp_path)
    collector = diagnostics.DiagnosticCollector(database, tmp_path, "parent")
    collector.collect()
    original = (tmp_path / "fleet-diagnostic-bundle.json").read_bytes()

    def fail(*args):
        raise ValueError("SECRET-CANARY")

    monkeypatch.setattr(diagnostics, "snapshot", fail)
    collector.collect()
    assert (tmp_path / "fleet-diagnostic-bundle.json").read_bytes() == original
    marker = (tmp_path / "fleet-diagnostic-export-error.json").read_text()
    assert "DIAGNOSTIC_EXPORT_FAILED" in marker
    assert "SECRET-CANARY" not in marker


def test_record_limits_are_explicit_and_reader_does_not_create_database(tmp_path, monkeypatch):
    missing = tmp_path / "missing.db"
    assert diagnostics.snapshot(missing, "parent")["collection_status"] == "database_not_created"
    assert not missing.exists()
    database = fixture_db(tmp_path)
    insert(database, "worker_result_v2", {"id": "result", "run_id": "child"})
    monkeypatch.setattr(diagnostics, "MAX_RECORDS", 1)
    bundle = diagnostics.snapshot(database, "parent")
    assert len(bundle["records"]) == 1
    assert bundle["truncated"]
    assert bundle["collection_status"] == "partial"


def test_projection_array_limits_are_reported(tmp_path):
    database = fixture_db(tmp_path)
    insert(
        database,
        "worker_result_v2",
        {"id": "result", "run_id": "child", "proposals": [{"id": "value"}] * 257},
    )
    bundle = diagnostics.snapshot(database, "parent")
    assert bundle["truncated"]
    assert bundle["projection_limits_reached"] == ["array_items"]


def test_exception_exit_retains_committed_diagnostics(tmp_path):
    database = fixture_db(tmp_path)
    with pytest.raises(RuntimeError), diagnostics.DiagnosticCollector(database, tmp_path, "parent"):
        raise RuntimeError("SECRET-CANARY")
    saved = (tmp_path / "fleet-diagnostic-bundle.json").read_text()
    assert "SECRET-CANARY" not in saved
    assert json.loads(saved)["state"] == "interrupted"


def test_node_execution_links_include_legacy_children_and_nested_runs(tmp_path):
    database = fixture_db(tmp_path)
    insert(
        database,
        "node_execution_v2",
        {"id": "legacy-link", "run_id": "parent", "work_run_id": "legacy"},
    )
    insert(
        database,
        "node_execution_v2",
        {"id": "nested-link", "run_id": "child", "work_run_id": "nested"},
    )
    for child in ("legacy", "nested", "unrelated"):
        insert(
            database,
            "worker_boundary_diagnostic_v2",
            {"id": child, "run_id": child, "code": "TIMEOUT"},
        )
    bundle = diagnostics.snapshot(database, "parent")
    assert set(bundle["related_run_ids"]) == {
        diagnostics.reference(name) for name in ("parent", "child", "legacy", "nested")
    }
    assert sum(item["kind"] == "worker_boundary_diagnostic_v2" for item in bundle["records"]) == 2


def test_safe_profile_projection_preserves_routing_and_timing():
    profile = {
        "choice": {"profile": "adaptive", "routing_mode": "adaptive"},
        "adaptive_execution": {
            "path": "direct",
            "decision_digest": "a" * 64,
            "reason": "SECRET-CANARY",
            "recommendation": {"path": "direct", "reason": "SECRET-CANARY"},
        },
        "effective_stages": [
            {"stage": "planning", "disposition": "omitted", "reason": "SECRET-CANARY"}
        ],
        "timings": [{"phase": "invocation", "seconds": 3.5}],
        "completed_invocation_wall_seconds": 3.5,
        "timing_complete": True,
    }
    projected = diagnostics.project(profile)
    assert projected["choice"] == profile["choice"]
    assert projected["adaptive_execution"]["path"] == "direct"
    assert projected["effective_stages"] == [{"stage": "planning", "disposition": "omitted"}]
    assert projected["timings"] == profile["timings"]
    assert projected["timing_complete"] is True
    assert "SECRET-CANARY" not in json.dumps(projected)


def test_database_disappearing_during_collection_preserves_last_evidence(tmp_path):
    database = fixture_db(tmp_path)
    collector = diagnostics.DiagnosticCollector(database, tmp_path, "parent")
    collector.collect()
    saved = (tmp_path / "fleet-diagnostic-bundle.json").read_bytes()
    database.unlink()
    collector.collect("controller_finished")
    assert (tmp_path / "fleet-diagnostic-bundle.json").read_bytes() == saved
    assert (tmp_path / "fleet-diagnostic-export-error.json").exists()
