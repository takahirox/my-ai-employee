"""Generic public CLI lifecycle; no external evaluation protocol or live model."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_employee.cli import PUBLIC_CONTRACT, main, projection
from ai_employee.history import Journal, Stopped
from ai_employee.models import Check, Clarification, Criterion, Usage

from .test_autonomous_runtime import OfflineModel, clarification, config, runtime


def test_separate_cli_processes_submit_inspect_cancel_cleanup_without_model(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "input.txt").write_text("original")
    cfg = tmp_path / "policy.json"
    cfg.write_text(config().model_dump_json())
    state = tmp_path / "state"

    def call(*args):
        process = subprocess.run(
            [sys.executable, "-m", "ai_employee", "--state", str(state), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert process.returncode == 0, process.stdout + process.stderr
        result = json.loads(process.stdout)
        assert result["contract_version"] == PUBLIC_CONTRACT
        return result

    run = call("submit", "Write result", "--config", str(cfg), "--root", str(source))["run_id"]
    view = call("status", run)
    assert view["goal"] is None and not view["stage_invocations"]
    assert call("history")["runs"][0]["run_id"] == run
    assert call("cancel", run)["cleanup"] == "not_requested"
    assert call("cleanup", run)["cleanup"] == "confirmed"
    assert call("cleanup", run)["status"] == "stopped"
    assert call("logs", run)["events"]
    assert (state / "history.db").exists()
    assert (source / "input.txt").read_text() == "original"


def test_public_result_cleanup_and_promotion_preserve_exact_completion(tmp_path, capsys):
    engine, source = runtime(tmp_path, OfflineModel())
    run = engine.start("Write result", config(), source)
    calls = len(engine.model.calls)
    with patch("ai_employee.cli.ContainerModel", return_value=engine.model):
        assert main(["--state", str(tmp_path), "result", run]) == 0
        result = json.loads(capsys.readouterr().out)
        assert main(["--state", str(tmp_path), "cleanup", run]) == 0
        view = json.loads(capsys.readouterr().out)
        assert view["status"] == "completed" and view["cleanup"] == "confirmed"
        destination = tmp_path / "published"
        assert (
            main(["--state", str(tmp_path), "promote", run, "--destination", str(destination)]) == 0
        )
        view = json.loads(capsys.readouterr().out)
        assert view["events"][-1]["body"]["candidate"] == result["candidate_digest"]
        assert result["candidate"] == engine.result(run).model_dump(mode="json")
        assert (destination / "result.txt").read_text() == "correct"
        assert (
            main(["--state", str(tmp_path), "promote", run, "--destination", str(destination)]) == 2
        )
    assert len(engine.model.calls) == calls


@pytest.mark.parametrize("terminal", ["failed", "uncertain", "stopped"])
def test_cleanup_preserves_terminal_outcome_and_refuses_unverified_result(tmp_path, terminal):
    model = OfflineModel()
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", config(), source)
    engine.journal.append(run, terminal, reason="fixture")
    before = projection(engine.journal, run)["status"]
    engine.cleanup(run)
    engine.cleanup(run)
    reopened = Journal(engine.journal.path)
    assert projection(reopened, run)["status"] == before
    assert projection(reopened, run)["cleanup"] == "confirmed"
    with pytest.raises(ValueError):
        engine.result(run)
    assert not model.calls


def test_busy_cleanup_requests_stop_but_never_claims_release(tmp_path):
    engine, source = runtime(tmp_path, OfflineModel())
    run = engine.prepare("Write result", config(), source)
    with engine.journal.controller(run):
        with pytest.raises(Stopped, match="RUN_ALREADY_OWNED"):
            engine.cleanup(run)
        assert projection(engine.journal, run)["cleanup"] == "pending"
        with pytest.raises(Stopped):
            engine.journal.append(run, "completed", candidate={})
    engine.cleanup(run)
    assert projection(engine.journal, run)["cleanup"] == "confirmed"
    assert not engine.model.calls


def test_failed_cleanup_can_be_repeated_without_model_or_outcome_loss(tmp_path):
    engine, source = runtime(tmp_path, OfflineModel())
    run = engine.prepare("Write result", config(), source)
    engine.journal.append(run, "failed", reason="original failure")
    with (
        patch.object(
            engine.model, "reconcile", side_effect=ValueError("RESOURCE_CLEANUP_UNCONFIRMED")
        ),
        pytest.raises(ValueError, match="RESOURCE_CLEANUP_UNCONFIRMED"),
    ):
        engine.cleanup(run)
    assert projection(engine.journal, run)["status"] == "failed"
    assert projection(engine.journal, run)["cleanup"] == "unconfirmed"
    engine.cleanup(run)
    assert projection(engine.journal, run)["cleanup"] == "confirmed"
    assert not engine.model.calls


def test_public_resume_usage_limit_remains_durable_and_never_retries(tmp_path, capsys):
    model = OfflineModel(quota=True)
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", config(), source)
    with patch("ai_employee.cli.ContainerModel", return_value=model):
        for _ in range(2):
            assert main(["--state", str(tmp_path), "resume", run]) == 2
            assert json.loads(capsys.readouterr().out)["run_id"] == run
        assert main(["--state", str(tmp_path), "inspect", run]) == 0
        view = json.loads(capsys.readouterr().out)
    assert model.calls == ["Clarification"]
    assert any(
        e["kind"] == "stopped" and "USAGE_LIMIT" in e["body"]["reason"] for e in view["events"]
    )


def test_operator_check_failure_blocks_publication_even_with_positive_model_review(tmp_path):
    class Checked(OfflineModel):
        def generate(self, policy, prompt, schema, *args, **kwargs):
            if schema is Clarification:
                return clarification().model_copy(
                    update={
                        "criteria": (
                            Criterion(
                                id="result", description="result exists", checks=("operator-check",)
                            ),
                        )
                    }
                ), Usage(tokens=0)
            return super().generate(policy, prompt, schema, *args, **kwargs)

        def check(self, argv, workspace, timeout, cancelled):
            result = subprocess.run(argv, cwd=workspace, capture_output=True, timeout=timeout)
            return result.returncode == 0, "operator check exit " + str(result.returncode)

    engine, source = runtime(tmp_path, Checked())
    cfg = config(replans=0, task_attempts=1).model_copy(
        update={
            "checks": (
                Check(
                    id="operator-check", argv=(sys.executable, "-I", "-c", "raise SystemExit(1)")
                ),
            ),
            "mandatory_checks": ("operator-check",),
        }
    )
    run = engine.prepare("Write result", cfg, source)
    with pytest.raises(ValueError):
        engine.execute(run)
    with pytest.raises(ValueError):
        engine.promote(run, tmp_path / "unverified")
    assert not (tmp_path / "unverified").exists()
    assert not (source / "result.txt").exists()


def test_cleanup_fences_a_resumed_failed_run_and_preserves_failure_display(tmp_path):
    engine, source = runtime(tmp_path, OfflineModel())
    run = engine.prepare("Write result", config(), source)
    engine.journal.append(run, "failed", reason="earlier attempt")
    with engine.journal.controller(run):
        with pytest.raises(Stopped, match="RUN_ALREADY_OWNED"):
            engine.cleanup(run)
        with pytest.raises(Stopped):
            engine.journal.reserve(run, "worker")
    engine.cleanup(run)
    assert projection(engine.journal, run)["status"] == "failed"
    assert projection(engine.journal, run)["cleanup"] == "confirmed"


def test_cli_cleanup_docker_timeout_is_durable_unconfirmed_error(tmp_path, capsys):
    engine, source = runtime(tmp_path, OfflineModel())
    run = engine.prepare("Write result", config(), source)
    directory = tmp_path / "workspaces" / run
    directory.mkdir(parents=True)
    (directory / "fixture.resources.jsonl").write_text(
        json.dumps(
            {
                "kind": "container",
                "name": "fleet-candidate-" + "a" * 32,
                "state": "created",
            }
        )
        + "\n"
    )
    with patch(
        "ai_employee.container.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 15)
    ):
        assert main(["--state", str(tmp_path), "cleanup", run]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "RESOURCE_CLEANUP_UNCONFIRMED"
    view = projection(engine.journal, run)
    assert view["cleanup"] == "unconfirmed"
    assert view["events"][-1]["body"]["reason"] == "RESOURCE_CLEANUP_UNCONFIRMED"


def test_cli_clarification_answer_resumes_same_run_and_gets_result(tmp_path, capsys):
    model = OfflineModel(ambiguous=True)
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", config(), source)
    with patch("ai_employee.cli.ContainerModel", return_value=model):
        assert main(["--state", str(tmp_path), "resume", run]) == 2
        assert json.loads(capsys.readouterr().out)["status"] == "waiting_for_clarification"
        assert main(["--state", str(tmp_path), "answer", run, "Use the requested result"]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "ready_to_resume"
        model.ambiguous = False
        assert main(["--state", str(tmp_path), "resume", run]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "completed"
        assert main(["--state", str(tmp_path), "result", run]) == 0
        assert json.loads(capsys.readouterr().out)["run_id"] == run
