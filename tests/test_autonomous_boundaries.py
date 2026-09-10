"""Boundary regressions: provider failures, local processes and immutable input."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_employee.candidates import Candidates
from ai_employee.history import Journal, Stopped
from ai_employee.models import Authority, Usage
from ai_employee.native import (
    codex_permissions,
    quota_error,
    run_process,
)

from .test_autonomous_runtime import OfflineModel, config, runtime


@pytest.mark.parametrize(
    "event",
    [
        {"type": "error", "error": {"code": "usage_limit_reached"}},
        {"type": "turn.failed", "error": {"message": "insufficient_quota"}},
        {"type": "result", "is_error": True, "result": "You've hit your limit"},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
    ],
)
def test_actual_provider_limit_frames_are_terminal(event: object) -> None:
    assert quota_error(json.dumps(event))


def test_quota_source_mentions_and_successful_tool_output_are_not_provider_limits() -> None:
    assert not quota_error("tests contain usage_limit_reached")
    assert not quota_error(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "aggregated_output": "usage_limit_reached"},
            }
        )
    )
    assert not quota_error(
        json.dumps(
            {"type": "result", "is_error": False, "result": "implemented rate_limit_error handling"}
        )
    )


def test_provider_error_without_final_newline_is_still_terminal(tmp_path: Path) -> None:
    with pytest.raises(Stopped, match="USAGE_LIMIT"):
        run_process(
            (
                sys.executable,
                "-c",
                'import sys; sys.stderr.write(\'{"type":"error","error":"insufficient_quota"}\')',
            ),
            tmp_path,
            2,
            lambda: False,
        )


def test_native_policy_does_not_grant_host_wide_reads_or_disable_sandbox(tmp_path: Path) -> None:
    args = codex_permissions(tmp_path, Authority())
    policy = " ".join(args)
    assert '":minimal"="read"' in policy
    assert '":root"' not in policy
    assert "danger-full-access" not in policy
    assert "network.enabled=false" in policy
    assert str(tmp_path / ".fleet-inputs") in policy
    assert '"deny"' in policy


def test_strict_external_guarantees_fail_before_native_or_model_execution(tmp_path: Path) -> None:
    with patch("ai_employee.native.run_process") as process:
        with pytest.raises(ValueError, match="REQUIRED_AUTHORITY_BOUNDARY_UNAVAILABLE"):
            from ai_employee.container import ContainerModel

            ContainerModel(None).apply_authority(
                tmp_path, Authority(duplicate_prevention=True), 30, lambda: False
            )
        process.assert_not_called()


def test_supervision_observes_progress_without_restarting_the_process(tmp_path: Path) -> None:
    observations: list[tuple[float, int]] = []
    code, output = run_process(
        (sys.executable, "-c", "import time; print('progress',flush=True); time.sleep(.3)"),
        tmp_path,
        2,
        lambda: False,
        supervision_seconds=0.05,
        observer=lambda elapsed, size: observations.append((elapsed, size)),
    )
    assert code == 0 and output.count("progress") == 1
    assert observations and observations[-1][1] > 0


def test_cancelled_process_cannot_finish_a_later_write(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    command = (
        sys.executable,
        "-c",
        "from pathlib import Path; import time; "
        "Path('started').write_text('yes'); time.sleep(10); Path('late').write_text('bad')",
    )
    with pytest.raises(Stopped):
        run_process(command, tmp_path, 2, marker.exists)
    assert not (tmp_path / "late").exists()


def test_repository_input_excludes_untracked_secrets(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "tracked.txt").write_text("public input")
    (source / "secret.env").write_text("disposable canary")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    candidates = Candidates(tmp_path / "objects")
    tree = candidates.capture_source(source)
    assert set(candidates.manifest(tree)) == {"tracked.txt"}


def test_unsettled_reservation_survives_controller_restart(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config(active_seconds=5, invocation_seconds=5))
    journal.reserve(run, "worker")
    reopened = Journal(journal.path)
    with pytest.raises(Stopped, match="RUN_BUDGET_EXHAUSTED"):
        reopened.reserve(run, "repair")


def test_usage_overrun_stops_other_reserved_workers(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config(tokens=20, reservation_tokens=10))
    first, _ = journal.reserve(run, "worker")
    journal.reserve(run, "worker")
    journal.settle(run, first, 0.1, Usage(tokens=30))
    with pytest.raises(Stopped, match="RUN_BUDGET_EXHAUSTED"):
        journal.check(run)


def test_original_input_is_checked_against_its_creation_digest(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config())
    with journal.connect() as db:
        db.execute("UPDATE runs SET original='different goal' WHERE id=?", (run,))
    with pytest.raises(ValueError, match="ORIGINAL_INPUT_CHANGED"):
        journal.original(run)


def test_controller_ownership_prevents_overlapping_execution(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config())
    with (
        journal.controller(run),
        pytest.raises(Stopped, match="RUN_ALREADY_OWNED"),
        journal.controller(run),
    ):
        pytest.fail("second owner acquired the run")


def test_completed_run_rejects_an_unrequested_clarification_answer(tmp_path: Path) -> None:
    engine, source = runtime(tmp_path, OfflineModel())
    run = engine.start("Write result", config(), source)
    with pytest.raises(ValueError, match="NO_PENDING_CLARIFICATION"):
        engine.answer(run, "weaken the criteria")


def test_resource_leases_serialize_overlap_but_allow_unrelated_hosts(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "history.db")
    first = journal.create("first", config())
    second = journal.create("second", config())
    a = Authority(network_hosts=("api.example.com",), external_writes=True)
    b = Authority(network_hosts=("other.example.com",), external_writes=True)
    wildcard = Authority(network_hosts=("*.example.com",), external_writes=True)
    assert journal.acquire_resources(first, "a", a)
    assert journal.acquire_resources(second, "b", b)
    assert not journal.acquire_resources(second, "a", a)
    assert not journal.acquire_resources(second, "wildcard", wildcard)
    # Reopening history does not infer release from an interrupted controller.
    reopened = Journal(journal.path)
    assert not reopened.acquire_resources(second, "a", a)
    reopened.release_resources(first, "a")
    assert reopened.acquire_resources(second, "a", a)


def test_authority_ceiling_cannot_drop_required_operation_controls() -> None:
    ceiling = Authority(external_writes=True, operation_approval=True, duplicate_prevention=True)
    assert not Authority(external_writes=True).within(ceiling)
    assert not Authority(external_writes=True, operation_approval=True).within(ceiling)
    assert ceiling.within(ceiling)


def test_malformed_rate_limit_event_is_not_an_exception() -> None:
    from ai_employee.native import quota_error

    assert not quota_error('{"type":"rate_limit_event","rate_limit_info":null}')


def test_observation_records_activity_without_secret_bodies(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    payload = {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "echo DISPOSABLE-SECRET",
            "aggregated_output": "DISPOSABLE-SECRET",
            "status": "completed",
            "exit_code": 0,
        },
    }
    code, _ = run_process(
        (sys.executable, "-c", f"print({json.dumps(payload)!r})"),
        tmp_path,
        2,
        lambda: False,
        observation=events.append,
    )
    assert code == 0
    assert events[0]["activity"] == "command_execution"
    assert events[0]["exit_code"] == 0
    assert "SECRET" not in json.dumps(events)


def test_missing_provider_usage_components_remain_unknown() -> None:
    from ai_employee.native import measured_tokens

    assert measured_tokens({"input_tokens": 10}) is None
    assert measured_tokens({"input_tokens": True, "output_tokens": 2}) is None
    assert measured_tokens({"input_tokens": 10, "output_tokens": 2}) == 12
    assert measured_tokens({"input_tokens": 10, "output_tokens": 2}, claude=True) is None


def test_native_commentary_does_not_replace_final_structured_result(tmp_path: Path) -> None:
    from ai_employee.models import WorkerResult

    frames = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Working on it."}},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": WorkerResult(status="completed", summary="done").model_dump_json(),
            },
        },
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
    ]
    from ai_employee.native import decode_response

    result, usage = decode_response("\n".join(map(json.dumps, frames)), WorkerResult)
    assert result.status == "completed" and usage.tokens == 12


def test_confirmed_resource_cleanup_is_durable_and_missing_network_is_not_failure(
    tmp_path: Path,
) -> None:
    from ai_employee.container import ContainerModel

    name = "fleet-candidate-" + "a" * 32 + "-network"
    ledger = tmp_path / "worker.resources.jsonl"
    ledger.write_text(json.dumps({"kind": "network", "name": name, "state": "created"}) + "\n")
    model = ContainerModel(None)
    missing = subprocess.CompletedProcess([], 1, b"", f"network {name} not found".encode())
    with patch("ai_employee.container.subprocess.run", return_value=missing) as command:
        model.reconcile(tmp_path)
        model.reconcile(tmp_path)
        assert command.call_count == 1
    assert json.loads(ledger.read_text().splitlines()[-1])["state"] == "removed"


def test_missing_resource_does_not_resolve_unconfirmed_creation(tmp_path: Path) -> None:
    from ai_employee.container import ContainerModel

    name = "fleet-candidate-" + "b" * 32
    ledger = tmp_path / "worker.resources.jsonl"
    ledger.write_text(json.dumps({"kind": "container", "name": name, "state": "intent"}) + "\n")
    missing = subprocess.CompletedProcess([], 1, b"", f"No such container: {name}".encode())
    with (
        patch("ai_employee.container.subprocess.run", return_value=missing),
        pytest.raises(ValueError, match="RESOURCE_CREATION_UNCERTAIN"),
    ):
        ContainerModel(None).reconcile(tmp_path)
    assert len(ledger.read_text().splitlines()) == 1
