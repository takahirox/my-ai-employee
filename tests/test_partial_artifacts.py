"""Actual snapshot transfer/storage at a mocked native boundary; no credentials or models."""

from __future__ import annotations

import json
import time

import pytest

from ai_employee.candidates import Candidates
from ai_employee.cli import main, projection
from ai_employee.container import ContainerModel
from ai_employee.engine import Engine
from ai_employee.history import Journal, Stopped
from ai_employee.isolated_worker import DockerCandidate, IsolatedBudgetExceeded
from ai_employee.models import WorkerResult
from ai_employee.snapshot import pack_workspace

from .test_autonomous_runtime import OfflineModel, config, runtime
from .test_execution_diagnostics import PROFILE
from .test_failure_finalization import lifecycle
from .test_stage_contracts import stream


def failing_worker(
    tmp_path, monkeypatch, failure="nonzero", *, cleanup_failure=False, unsafe=None, revisions=0
):
    events = []
    lifecycle(monkeypatch, events, cleanup_failure=cleanup_failure)
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "edited.txt").write_text("actual unfinished work")
    (remote / "link.txt").symlink_to("edited.txt")
    (remote / ".codex").mkdir()
    (remote / ".codex/auth.json").write_text("excluded fixture credential")
    if unsafe == "symlink":
        (remote / "escape").symlink_to("/etc/passwd")
    elif unsafe == "size":
        (remote / "too-large").write_bytes(b"x" * 4096)
    adapter = ContainerModel(PROFILE, snapshot_max_bytes=1024 if unsafe == "size" else 100_000)
    monkeypatch.setattr(adapter, "_native_probe", lambda *a: None)
    monkeypatch.setattr(DockerCandidate, "_check_owner", lambda c: None)
    monkeypatch.setattr(DockerCandidate, "quiesce", lambda c: events.append("quiesce"))

    def docker(candidate, *args, **kwargs):
        if "pack_workspace" in args[-1]:
            events.append("extract")
            assert candidate.created
            assert candidate.native_completion["cleanup"] == "confirmed"
            if failure != "validation":
                assert candidate.deadline <= time.monotonic() + 30
            if failure == "extraction_timeout":
                raise TimeoutError("fixture")
            return pack_workspace(remote, adapter.snapshot_max_bytes)
        return b""

    def guarded(candidate, *args, **kwargs):
        candidate.begin_execution(kwargs["phase"])
        events.append("stopped")
        interruptions = {
            "timeout": TimeoutError("fixture"),
            "cancelled": TimeoutError("fixture cancelled"),
            "quota": Stopped("USAGE_LIMIT"),
            "output_limit": IsolatedBudgetExceeded("fixture"),
            "transport": ConnectionError("fixture"),
        }
        if failure in interruptions:
            raise interruptions[failure]
        if failure == "unknown":
            raise RuntimeError("ISOLATION_PROCESS_GUARD_FAILED")
        candidate.native_completion = {
            "root_exit": 7 if failure == "nonzero" else 0,
            "cleanup": "confirmed",
        }
        candidate.execution_diagnostic["native_exit_code"] = candidate.native_completion[
            "root_exit"
        ]
        if failure == "deleted":
            candidate.created = False
            raise RuntimeError("fixture environment lost")
        if failure == "decode" or unsafe or failure == "extraction_timeout":
            return 0, b"malformed response", b""
        return (
            int(candidate.native_completion["root_exit"]),
            stream(WorkerResult(status="completed", summary="done").model_dump()).encode(),
            b"",
        )

    monkeypatch.setattr(DockerCandidate, "_docker", docker)
    monkeypatch.setattr(DockerCandidate, "run_guarded", guarded)

    class Model(OfflineModel):
        def generate(self, *args, **kwargs):
            if args[2] is WorkerResult:
                if failure in {"decode", "nonzero"}:
                    assert (args[3] / "original.txt").read_text() == "keep"
                    assert not (args[3] / "edited.txt").exists()
                result, usage = adapter.generate(*args, **kwargs)
                if failure == "validation":
                    result = result.model_copy(update={"summary": "x" * 20001})
                return result, usage
            return super().generate(*args, **kwargs)

    engine, source = runtime(tmp_path, Model())
    cfg = config()
    cfg = cfg.model_copy(update={"worker": cfg.worker.model_copy(update={"revisions": revisions})})
    run = engine.prepare("Write result", cfg, source)
    return engine, run, events


@pytest.mark.parametrize("failure", ["nonzero", "decode", "validation"])
def test_failed_edits_survive_disposal_restart_and_explicit_export(tmp_path, monkeypatch, failure):
    engine, run, order = failing_worker(tmp_path, monkeypatch, failure)
    with pytest.raises(RuntimeError):
        engine.execute(run)
    engine = Engine(
        Journal(engine.journal.path), Candidates(engine.candidates.root), engine.model, engine.root
    )
    partials = engine.partials(run)
    assert len(partials) == 1
    artifact = partials[0]
    assert artifact["status"] == "saved" and artifact["verified"] is False
    assert artifact["run_id"] == run and artifact["task"] == "write" and artifact["attempt"]
    assert order.index("stopped") < order.index("extract") < order.index("dispose")
    assert order.count("extract") == 1
    exported = engine.export_partial(run, artifact["reservation"], tmp_path / "unverified")
    assert exported["verified"] is False
    assert (tmp_path / "unverified/edited.txt").read_text() == "actual unfinished work"
    assert (tmp_path / "unverified/link.txt").is_symlink()
    assert not (tmp_path / "unverified/.codex").exists()
    assert not (tmp_path / "unverified/original.txt").exists()
    assert projection(engine.journal, run)["partial_artifacts"] == partials
    with pytest.raises(ValueError, match="TARGET_EXISTS"):
        engine.export_partial(run, artifact["reservation"], tmp_path / "unverified")
    with pytest.raises(ValueError):
        engine.result(run)
    with pytest.raises(ValueError):
        engine.promote(run, tmp_path / "published")
    assert not any(
        e["kind"] in {"accepted", "completed", "promoted"} for e in engine.journal.events(run)
    )


@pytest.mark.parametrize(
    "failure,reason",
    [
        (name, "stop_unconfirmed")
        for name in ("unknown", "timeout", "cancelled", "quota", "output_limit", "transport")
    ]
    + [("deleted", "environment_unavailable")],
)
def test_unavailable_retention_never_captures_baseline(tmp_path, monkeypatch, failure, reason):
    engine, run, order = failing_worker(tmp_path, monkeypatch, failure)
    with pytest.raises(RuntimeError):
        engine.execute(run)
    (artifact,) = engine.partials(run)
    assert artifact["status"] == "unavailable" and artifact["reason"] == reason
    assert "tree" not in artifact and "extract" not in order
    assert order[-1] == "dispose"
    with pytest.raises(ValueError, match="PARTIAL_ARTIFACT_UNAVAILABLE"):
        engine.export_partial(run, artifact["reservation"], tmp_path / "bad")


@pytest.mark.parametrize("unsafe", ["symlink", "size", None])
def test_capture_failure_preserves_original_error_and_disposes(tmp_path, monkeypatch, unsafe):
    engine, run, order = failing_worker(
        tmp_path, monkeypatch, "decode" if unsafe else "extraction_timeout", unsafe=unsafe
    )
    with pytest.raises(RuntimeError):
        engine.execute(run)
    (artifact,) = engine.partials(run)
    assert artifact["status"] == "capture_failed" and "tree" not in artifact
    assert artifact["failure"] == "response_invalid"
    assert order[-1] == "dispose"


def test_cleanup_failure_does_not_erase_saved_partial_or_become_success(tmp_path, monkeypatch):
    engine, run, order = failing_worker(tmp_path, monkeypatch, cleanup_failure=True)
    with pytest.raises(RuntimeError, match="fixture disposal failed"):
        engine.execute(run)
    (artifact,) = engine.partials(run)
    assert artifact["status"] == "saved" and artifact["failure"] == "native_nonzero"
    diagnostics = json.dumps([e for e in engine.journal.events(run) if e["kind"] == "diagnostic"])
    assert "unconfirmed" in diagnostics
    assert order[-1] == "dispose"


def test_storage_failure_keeps_primary_failure_and_settlement(tmp_path, monkeypatch):
    engine, run, order = failing_worker(tmp_path, monkeypatch)
    capture = engine.candidates.capture

    def fail_partial(path):
        if (path / "edited.txt").exists():
            raise OSError("fixture storage unavailable")
        return capture(path)

    monkeypatch.setattr(engine.candidates, "capture", fail_partial)
    with pytest.raises(RuntimeError, match="ENVIRONMENT_OR_INVARIANT_FAILURE"):
        engine.execute(run)
    (artifact,) = engine.partials(run)
    assert artifact["status"] == "capture_failed" and artifact["reason"] == "OSError"
    assert order[-1] == "dispose"
    assert len([e for e in engine.journal.events(run) if e["kind"] == "settled"]) == len(
        [e for e in engine.journal.events(run) if e["kind"] == "reserved"]
    )


def test_cli_lists_and_exports_without_promotion(tmp_path, monkeypatch, capsys):
    engine, run, _ = failing_worker(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError):
        engine.execute(run)
    assert main(["--state", str(tmp_path), "partials", run]) == 0
    output = json.loads(capsys.readouterr().out)
    (artifact,) = output["partial_artifacts"]
    destination = tmp_path / "explicit-unverified"
    assert (
        main(
            [
                "--state",
                str(tmp_path),
                "export-partial",
                run,
                "--reservation",
                artifact["reservation"],
                "--destination",
                str(destination),
            ]
        )
        == 0
    )
    exported = json.loads(capsys.readouterr().out)
    assert exported["verified"] is False and exported["tree"] == artifact["tree"]
    assert (destination / "edited.txt").read_text() == "actual unfinished work"
    assert (
        main(
            [
                "--state",
                str(tmp_path),
                "export-partial",
                run,
                "--reservation",
                artifact["reservation"],
                "--destination",
                str(destination),
            ]
        )
        == 2
    )
    assert "TARGET_EXISTS" in json.loads(capsys.readouterr().out)["error"]


def test_parallel_tasks_and_attempts_keep_separate_partial_identities(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from ai_employee.diagnostics import attach_failure

    engine, source = runtime(tmp_path, OfflineModel())
    run = engine.prepare("Write result", config(), source)

    def retain(index):
        workspace = tmp_path / str(index)
        workspace.mkdir()
        (workspace / "edit").write_text(str(index))
        error = RuntimeError("fixture")
        attach_failure(
            error,
            {
                "workspace_returned": True,
                "termination": {
                    "phase": "model",
                    "process_stop": "confirmed",
                    "reason": "response_invalid",
                    "environment": str(index),
                },
            },
        )
        engine._retain_partial(
            run,
            "reservation-" + str(index),
            workspace,
            error,
            None,
            {"task": "task-" + str(index % 2), "attempt": str(index)},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(retain, range(4)))
    saved = engine.partials(run)
    assert len({p["tree"] for p in saved}) == len({p["reservation"] for p in saved}) == 4
    for artifact in saved:
        target = tmp_path / ("export-" + artifact["attempt"])
        engine.export_partial(run, artifact["reservation"], target)
        assert (target / "edit").read_text() == artifact["attempt"]
    assert not any(
        e["kind"] in {"candidate", "accepted", "completed"} for e in engine.journal.events(run)
    )


def test_changed_partial_storage_cannot_be_exported(tmp_path, monkeypatch):
    engine, run, _ = failing_worker(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError):
        engine.execute(run)
    (artifact,) = engine.partials(run)
    manifest = engine.candidates.root / artifact["tree"] / "manifest.json"
    manifest.write_text("{}")
    with pytest.raises(ValueError, match="MANIFEST_CHANGED"):
        engine.export_partial(run, artifact["reservation"], tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_probe_completion_does_not_trigger_partial_extraction(tmp_path, monkeypatch):
    lifecycle(monkeypatch, [])
    model = ContainerModel(PROFILE)
    monkeypatch.setattr(
        model, "_copy_workspace", lambda *a: pytest.fail("probe is not worker evidence")
    )
    with (
        pytest.raises(RuntimeError),
        model._candidate(
            tmp_path, 30, lambda: False, models=False, retain_partial=lambda body: None
        ) as candidate,
    ):
        candidate.begin_execution("probe")
        candidate.native_completion = {"root_exit": 0, "cleanup": "confirmed"}
        raise RuntimeError("fixture")
    assert candidate.execution_diagnostic["partial_workspace"] == {
        "status": "unavailable",
        "reason": "worker_not_started",
    }


def test_partial_record_failure_does_not_mask_primary_or_skip_cleanup(tmp_path, monkeypatch):
    engine, run, order = failing_worker(tmp_path, monkeypatch)
    append = engine.journal.append

    def failing_append(run_id, kind, **body):
        if kind == "partial_artifact":
            raise OSError("fixture journal full")
        return append(run_id, kind, **body)

    monkeypatch.setattr(engine.journal, "append", failing_append)
    with pytest.raises(RuntimeError, match="ENVIRONMENT_OR_INVARIANT_FAILURE"):
        engine.execute(run)
    assert not engine.partials(run)
    diagnostics = json.dumps([e for e in engine.journal.events(run) if e["kind"] == "diagnostic"])
    assert "artifact_record_unavailable" in diagnostics
    assert order[-1] == "dispose"


def test_extraction_that_outlives_deadline_is_not_saved(tmp_path, monkeypatch):
    lifecycle(monkeypatch, [])
    model = ContainerModel(PROFILE)

    def slow_copy(candidate, workspace):
        candidate.execution_diagnostic["workspace_returned"] = True
        candidate.deadline = time.monotonic() - 1

    monkeypatch.setattr(model, "_copy_workspace", slow_copy)
    with (
        pytest.raises(ValueError, match="WORKER_PROCESS_FAILED"),
        model._candidate(
            tmp_path, 30, lambda: False, models=False, retain_partial=lambda body: None
        ) as candidate,
    ):
        candidate.begin_execution("model")
        candidate.native_completion = {"root_exit": 1, "cleanup": "confirmed"}
        raise ValueError("WORKER_PROCESS_FAILED")
    assert candidate.execution_diagnostic["partial_workspace"]["status"] == "capture_failed"
    assert candidate.disposal == "confirmed"


def test_output_repair_does_not_reuse_partial_workspace(tmp_path, monkeypatch):
    engine, run, order = failing_worker(tmp_path, monkeypatch, "decode", revisions=1)
    with pytest.raises(RuntimeError, match="OUTPUT_REPAIR_EXHAUSTED"):
        engine.execute(run)
    saved = engine.partials(run)
    assert len(saved) == 2
    assert all(p["status"] == "saved" for p in saved)
    assert len({p["reservation"] for p in saved}) == 2
    assert order.count("extract") == order.count("dispose") == 2
    assert not list(engine.root.glob("**/fleet-partial-*"))
