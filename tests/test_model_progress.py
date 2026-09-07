import json
import sys
from pathlib import Path

from ai_employee.domain.v2 import ProcessRequest
from ai_employee.model_progress import ModelProgressRecord, progress_observer
from ai_employee.model_usage import filter_model_stdout
from ai_employee.serialization import canonical_json
from ai_employee.services_v2 import AtomicArtifactStore, LocalProcessExecutor
from ai_employee.services_v2._common import now
from ai_employee.storage import SQLiteStore
from tests.test_controlled_services_v2 import NeverCancelled, allow


def request(**kwargs):
    return ProcessRequest(
        id="model-test",
        run_id="child",
        created_at=now(),
        argv=("/usr/bin/printf", "hello"),
        timeout_seconds=1.0,
        purpose="obtain strict worker proposal envelope",
        **kwargs,
    )


def test_progress_is_body_free_timed_bounded_and_durable(tmp_path):
    req = request()
    with SQLiteStore(tmp_path / "state.db") as store:
        observer = progress_observer(store, "graph", req)
        observer(b'{"type":{}}\n', 0.0)
        event = {
            "type": "item.started",
            "item": {"id": "secret-id", "type": "command_execution", "command": "secret-command"},
        }
        data = (json.dumps(event) + "\n").encode()
        observer(data[:10], 0.1)
        observer(data[10:], 0.2)
        observer(
            b'{"type":"item.completed","item":{"id":"secret-id",'
            b'"type":"command_execution","exit_code":0,"aggregated_output":"secret-output"}}\n',
            1.2,
        )
        records = store.list_records("model_progress_v2", ModelProgressRecord, run_id="graph")
        completed = next(r for r in records if r.event == "item.completed")
        assert completed.item_duration_seconds == 1.0
        assert "secret" not in canonical_json(records)
        for _ in range(200):
            observer(b'{"type":"turn.started"}\n', 2.0)
    with SQLiteStore(tmp_path / "state.db") as store:
        records = store.list_records("model_progress_v2", ModelProgressRecord, run_id="graph")
        assert len(records) == 128
        assert max(r.events_observed for r in records) == 202
        assert any(r.history_truncated for r in records)


def test_oversized_stream_resynchronizes_without_body_persistence(tmp_path):
    with SQLiteStore(tmp_path / "state.db") as store:
        observer = progress_observer(store, "graph", request())
        observer(b"x" * 70000, 0.1)
        observer(b'ignored\n{"type":"turn.started"}\n', 0.2)
        assert len(store.list_records("model_progress_v2", ModelProgressRecord)) == 1


def test_live_event_survives_timeout_before_final_output(tmp_path):
    script = tmp_path / "fixture.py"
    script.write_text(
        'import time\nprint(\'{"type":"item.started","item":'
        '{"id":"x","type":"command_execution","command":"secret"}}\',flush=True)\n'
        "time.sleep(10)\n"
    )
    req = request().model_copy(
        update={"argv": (sys.executable, str(script)), "timeout_seconds": 0.3}
    )
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    with SQLiteStore(tmp_path / "state.db") as store:
        executor = LocalProcessExecutor(
            (tmp_path,),
            artifacts,
            executable_paths=(Path(sys.executable).resolve().parent,),
            stdout_storage_filter=lambda r, b: filter_model_stdout("codex_cli", r, b),
            stdout_observer_factory=lambda r: progress_observer(store, "graph", r),
        )
        result = executor.execute(req, allow(req.content_digest), NeverCancelled())
        assert result.status == "failed" and result.failure.code.value == "TIMEOUT"
        rows = store.list_records("model_progress_v2", ModelProgressRecord, run_id="graph")
        assert len(rows) == 1 and rows[0].event == "item.started"
        assert "secret" not in canonical_json(rows)
