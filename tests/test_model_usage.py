from __future__ import annotations

import json

import pytest

from ai_employee.domain.v2 import DecisionOutcome, ExecutionResult, PolicyDecision, ProcessRequest
from ai_employee.model_usage import (
    UsageRecordingExecutor,
    UsageTotals,
    codex_payload,
    inspect_usage,
    provider_usage,
    record_native_usage,
    summarize_usage,
)
from ai_employee.prompt_transport import prompt_json
from ai_employee.serialization import canonical_json
from ai_employee.services_v2._common import now
from ai_employee.storage import SQLiteStore
from ai_employee.worker_adapters import _bounded_prompt
from tests.test_work_orchestration_v2 import worker_request


def stream(*samples):
    return "\n".join(json.dumps({"type": "turn.completed", "usage": sample}) for sample in samples)


def test_codex_usage_comes_only_from_transport_and_sums_completed_turns():
    samples = (
        {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 3},
        {"input_tokens": 30, "cached_input_tokens": 20, "output_tokens": 4},
    )
    assert provider_usage("codex_cli", stream(*samples)) == {
        "usage_source": "codex_turn_completed",
        "input_tokens": 130,
        "cached_input_tokens": 100,
        "output_tokens": 7,
        "cache_write_tokens": None,
        "reasoning_output_tokens": None,
    }
    for backend in ("codex_cli", "claude_code_cli", "ollama_cli"):
        assert provider_usage(backend, '{"usage":{"input_tokens":9999}}') == {}
    fake = {"type": "item.completed", "item": {"type": "agent_message", "text": stream(*samples)}}
    assert provider_usage("codex_cli", json.dumps(fake)) == {}


@pytest.mark.parametrize("bad", [None, True, -1, "100", 1.5])
def test_bad_or_missing_counts_remain_unavailable(bad):
    usage = provider_usage("codex_cli", stream({"input_tokens": bad, "output_tokens": 0}))
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] == 0


def test_invalid_cached_counts_and_incomplete_totals_do_not_invent_ratios():
    usage = provider_usage("codex_cli", stream({"input_tokens": 10, "cached_input_tokens": 11}))
    assert usage["cached_input_tokens"] is None
    usage = provider_usage("codex_cli", stream({"input_tokens": 10}, {"output_tokens": 1}))
    assert usage["input_tokens"] is None


def test_claude_cache_categories_are_added_to_input_and_cost_is_a_frozen_estimate():
    usage = provider_usage(
        "claude_code_cli",
        json.dumps(
            {
                "type": "result",
                "total_cost_usd": 0.02,
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 80,
                    "cache_creation_input_tokens": 20,
                    "output_tokens": 7,
                },
                "structured_output": {"usage": {"input_tokens": 9999}},
                "secret": "do-not-copy",
            }
        ),
    )
    assert usage["input_tokens"] == 110 and usage["cached_input_tokens"] == 80
    assert usage["cost_usd"] == 0.02 and usage["cost_kind"] == "cli_estimate"
    assert usage["scope"] == "main_cli_loop"
    assert "not billed cost" in usage["cost_basis"]
    assert "do-not-copy" not in canonical_json(usage)


@pytest.mark.parametrize("cost", [True, -1, float("inf"), float("nan"), "1.0"])
def test_invalid_cost_is_unavailable(cost):
    assert "cost_usd" not in provider_usage(
        "claude_code_cli", json.dumps({"type": "result", "total_cost_usd": cost})
    )


def test_codex_message_decoding_requires_completed_transport_not_tool_output():
    output = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started"},
            {"type": "item.completed", "item": {"type": "command_execution", "text": "bad"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok":true}'}},
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        )
    )
    assert codex_payload(output) == '{"ok":true}'
    assert codex_payload('{"ok":true}') == '{"ok":true}'
    for suffix in ('{"type":"turn.failed"}', '{"type":"error"}'):
        with pytest.raises(ValueError):
            codex_payload(output + "\n" + suffix)
    with pytest.raises(ValueError):
        codex_payload(output.rsplit("\n", 1)[0])


def test_recording_is_idempotent_body_free_and_survives_reopening(tmp_path):
    request = ProcessRequest(
        id="model-call",
        run_id="child",
        created_at=now(),
        argv=("codex", "exec", "--json"),
        cwd=".",
        purpose="obtain strict worker proposal envelope",
        timeout_seconds=1.0,
    )
    result = ExecutionResult(
        id="result",
        run_id="child",
        created_at=now(),
        request_digest=request.content_digest,
        status="succeeded",
        duration_seconds=0.1,
        stdout_artifact_digest="1" * 64,
    )

    class Executor:
        def execute(self, *_):
            return result

    decision = PolicyDecision(
        id="decision",
        run_id="child",
        created_at=now(),
        request_digest=request.content_digest,
        outcome=DecisionOutcome.ALLOW,
        effective_policy_digest="2" * 64,
        reason_code="fixture",
    )

    class Cancellation:
        def cancelled(self):
            return False

    with SQLiteStore(tmp_path / "usage.db") as store:
        recorder = UsageRecordingExecutor(
            Executor(),
            store,
            lambda _: stream(
                {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 2}
            ).encode(),
            graph_run_id="graph",
            backend="codex_cli",
            model="fixture",
            effort="low",
            configuration_digest="3" * 64,
        )
        assert recorder.execute(request, decision, Cancellation()) is result
        assert recorder.execute(request, decision, Cancellation()) is result
    with SQLiteStore(tmp_path / "usage.db") as store:
        report = inspect_usage(store, ("graph", "graph"))
        assert report["invocations"] == 1
        assert report["metrics"]["input_tokens"]["total"] == 100
        assert report["cache_hit_ratio"] == 0.4
        assert report["metrics"]["cost_usd"]["total"] is None
        assert report["by_stage"]["worker"]["invocations"] == 1
        assert "argv" not in canonical_json(report)
        assert inspect_usage(store, ("foreign",))["invocations"] == 0


def test_unknown_calls_make_partial_totals_explicit_and_native_history_is_retained(tmp_path):
    with SQLiteStore(tmp_path / "usage.db") as store:
        request = worker_request()
        record_native_usage(
            store,
            "graph",
            "3" * 64,
            "fixture",
            "low",
            request,
            {"input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 1},
            1.0,
            "failed",
        )
        records = store.list_records("model_usage_v2", UsageTotals, run_id="graph")
        missing = UsageTotals(
            id="unknown",
            run_id="child",
            created_at=now(),
            graph_run_id="graph",
            backend="ollama_cli",
            model="fixture",
            effort=None,
            stage="planning",
            request_digest="4" * 64,
            configuration_digest="3" * 64,
            status="failed",
            duration_seconds=0.5,
        )
        report = summarize_usage((*records, missing))
        assert report["metrics"]["input_tokens"] == {
            "total": None,
            "known_subtotal": 10,
            "reported_invocations": 1,
        }
        assert report["cache_hit_ratio"] is None
        assert report["invocations"] == 2


def test_stable_prompt_order_preserves_schema_and_record_canonicalization():
    first = {
        "attempt": 1,
        "instruction": "stable",
        "response_schema": {"z": 1, "a": 2},
        "goal": "one",
    }
    second = {
        "goal": "two",
        "response_schema": {"a": 2, "z": 1},
        "instruction": "stable",
        "attempt": 2,
    }
    prefix = '{"instruction":"stable","response_schema":{"a":2,"z":1},'
    assert prompt_json(first).startswith(prefix)
    assert prompt_json(second).startswith(prefix)
    assert json.loads(prompt_json(first)) == first
    assert canonical_json(first).startswith('{"attempt":1,')
    assert prompt_json(dict(reversed(list(first.items())))) == prompt_json(first)


def test_actual_worker_prefix_is_stable_across_goals_and_repair_attempts():
    first = worker_request("first task")
    second = first.model_copy(update={"goal": "another task", "attempt": 2})
    left, right = _bounded_prompt(first).decode(), _bounded_prompt(second).decode()
    assert (
        left.split(',"accepted_feedback_digests":')[0]
        == right.split(',"accepted_feedback_digests":')[0]
    )
    assert left.index('"instruction"') < left.index('"goal"')
    assert json.loads(left)["goal"] == "first task"


def test_jsonl_tool_traces_are_removed_before_artifact_persistence(tmp_path):
    from ai_employee.model_usage import filter_model_stdout
    from ai_employee.services_v2 import AtomicArtifactStore, LocalProcessExecutor
    from tests.test_controlled_services_v2 import NeverCancelled, allow

    output = "\n".join(
        json.dumps(event)
        for event in (
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "aggregated_output": "tool-secret-canary"},
            },
            {"type": "item.completed", "item": {"type": "reasoning", "text": "reasoning-canary"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok":true}'}},
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 100, "output_tokens": 2, "private": "usage-canary"},
            },
        )
    )
    request = ProcessRequest(
        id="process-1",
        run_id="run-1",
        created_at=now(),
        argv=("/usr/bin/printf", "%s", output),
        timeout_seconds=1.0,
        stdout_bytes=10000,
        stderr_bytes=100,
        purpose="obtain strict worker proposal envelope",
    )
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    executor = LocalProcessExecutor(
        (tmp_path,),
        artifacts,
        stdout_storage_filter=lambda req, data: filter_model_stdout("codex_cli", req, data),
    )
    result = executor.execute(request, allow(request.content_digest), NeverCancelled())
    assert result.status == "succeeded"
    descriptor = executor.output_descriptor(
        result.stdout_artifact_digest, "process_stdout", result.id
    )
    stored = artifacts.open_verified(descriptor).read().decode()
    assert "canary" not in stored
    assert codex_payload(stored) == '{"ok":true}'
    assert provider_usage("codex_cli", stored)["input_tokens"] == 100
    assert result.resource_usage["stdout_bytes"] == len(output.encode())


def test_failed_usage_is_a_subtotal_not_a_complete_stage_total(tmp_path):
    with SQLiteStore(tmp_path / "usage.db") as store:
        request = worker_request()
        for _ in range(2):
            record_native_usage(
                store,
                "graph",
                "3" * 64,
                "fixture",
                "low",
                request,
                {"input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 1},
                1.0,
                "failed",
            )
        report = inspect_usage(store, ("graph",))
        assert report["invocations"] == 2
        assert report["metrics"]["input_tokens"]["total"] is None
        assert report["metrics"]["input_tokens"]["known_subtotal"] == 20
        assert report["by_stage"]["worker"]["input_tokens"] is None


def test_streaming_discards_large_tools_before_retained_limit(tmp_path):
    import sys

    from ai_employee.model_usage import filter_model_stdout, model_stdout_filter
    from ai_employee.services_v2 import AtomicArtifactStore, LocalProcessExecutor
    from tests.test_controlled_services_v2 import NeverCancelled, allow

    producer = tmp_path / "producer.py"
    producer.write_text(
        "import json\n"
        'print(json.dumps({"type":"item.completed","item":'
        '{"type":"command_execution","aggregated_output":"x"*1616047}}))\n'
        'print(json.dumps({"type":"item.completed","item":'
        '{"type":"agent_message","text":"{}"}}))\n'
        'print(json.dumps({"type":"turn.completed","usage":'
        '{"input_tokens":10,"output_tokens":1}}))\n'
    )
    request = ProcessRequest(
        id="large-stream",
        run_id="run",
        created_at=now(),
        argv=(sys.executable, str(producer)),
        timeout_seconds=5.0,
        stdout_bytes=1_000_000,
        stderr_bytes=1000,
        purpose="obtain strict worker proposal envelope",
    )
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    executor = LocalProcessExecutor(
        (tmp_path,),
        artifacts,
        executable_paths=(str(__import__("pathlib").Path(sys.executable).resolve().parent),),
        stdout_storage_filter=lambda req, data: filter_model_stdout("codex_cli", req, data),
        stdout_stream_filter_factory=lambda req: model_stdout_filter("codex_cli", req),
    )
    result = executor.execute(request, allow(request.content_digest), NeverCancelled())
    assert result.status == "succeeded"
    assert result.resource_usage["stdout_bytes"] > 1_600_000
    assert result.resource_usage["stdout_retained_bytes"] < 1000
    with artifacts.open_verified(
        executor.output_descriptor(result.stdout_artifact_digest, "process_stdout", result.id)
    ) as f:
        output = f.read().decode()
    assert codex_payload(output) == "{}"
    assert provider_usage("codex_cli", output)["input_tokens"] == 10


def test_stream_filter_bounds_partial_lines_and_preserves_failures():
    from ai_employee.model_usage import model_stdout_filter

    request = ProcessRequest(
        id="stream",
        run_id="run",
        created_at=now(),
        argv=("fixture",),
        timeout_seconds=1.0,
        stdout_bytes=1000,
        stderr_bytes=100,
        purpose="obtain strict worker proposal envelope",
    )
    filt = model_stdout_filter("codex_cli", request)
    assert filt is not None
    assert filt.feed(b"x" * 4001) == b""
    assert filt.exceeded and not filt.pending
    filt = model_stdout_filter("codex_cli", request)
    output = filt.feed(b'{"type":"error","message":"connection disconnect secret-canary"}\n')
    assert b"transport" in output and b"canary" not in output
    assert b"invalid_json" in filt.feed(b"not-json\n")
    assert (
        model_stdout_filter("codex_cli", request.model_copy(update={"purpose": "ordinary check"}))
        is None
    )


def test_process_diagnostics_keep_failure_class_without_provider_body(tmp_path):
    from ai_employee.domain.base import freeze_json
    from ai_employee.model_usage import ModelProcessDiagnostic

    request = ProcessRequest(
        id="failed-review",
        run_id="run-1",
        created_at=now(),
        argv=("fixture",),
        timeout_seconds=1.0,
        purpose="obtain a strict non-authoritative PlanReviewPayload",
    )
    result = ExecutionResult(
        id="result-1",
        run_id="run-1",
        created_at=now(),
        request_digest=request.content_digest,
        status="succeeded",
        exit_code=0,
        duration_seconds=0.1,
        stdout_artifact_digest="a" * 64,
        resource_usage=freeze_json({"stdout_bytes": 101, "private": "secret-canary"}),
    )

    class Executor:
        def execute(self, *args):
            return result

    with SQLiteStore(tmp_path / "state.db") as store:
        executor = UsageRecordingExecutor(
            Executor(),
            store,
            lambda _: b'{"type":"error","message":"connection disconnected secret-canary"}',
            graph_run_id="graph",
            backend="codex_cli",
            model="fixture",
            effort="high",
            configuration_digest="b" * 64,
        )
        executor.execute(
            request,
            PolicyDecision(
                id="decision",
                run_id="run-1",
                created_at=now(),
                request_digest=request.content_digest,
                effective_policy_digest="0" * 64,
                outcome=DecisionOutcome.ALLOW,
                reason_code="allowed",
            ),
            None,
        )
        records = store.list_records(
            "model_process_diagnostic_v2", ModelProcessDiagnostic, run_id="graph"
        )
        assert len(records) == 1
        assert records[0].stage == "plan_review"
        assert records[0].transport_failures == ("transport",)
        assert records[0].resource_usage == {"stdout_bytes": 101}
        assert "canary" not in canonical_json(records[0])


@pytest.mark.parametrize("case", ["raw", "event", "retained", "stderr"])
def test_streamed_output_still_stops_at_each_finite_limit(tmp_path, case):
    import sys
    from pathlib import Path

    from ai_employee.model_usage import model_stdout_filter
    from ai_employee.services_v2 import AtomicArtifactStore, LocalProcessExecutor
    from tests.test_controlled_services_v2 import NeverCancelled, allow

    sources = {
        "raw": (
            'for _ in range(500): print(json.dumps({"type":"item.completed","item":'
            '{"type":"command_execution","aggregated_output":"x"*100}}))'
        ),
        "event": (
            'print(json.dumps({"type":"item.completed","item":'
            '{"type":"command_execution","aggregated_output":"x"*5000}}))'
        ),
        "retained": (
            'print(json.dumps({"type":"item.completed","item":'
            '{"type":"agent_message","text":"x"*2000}}))'
        ),
        "stderr": 'sys.stderr.write("x"*2000);sys.stderr.flush()',
    }
    producer = tmp_path / "producer.py"
    producer.write_text(
        "import json,sys,time\n" + sources[case] + '\nprint("",flush=True)\ntime.sleep(10)\n'
    )
    request = ProcessRequest(
        id="bounded",
        run_id="run",
        created_at=now(),
        argv=(sys.executable, str(producer)),
        stdout_bytes=1000,
        stderr_bytes=1000,
        timeout_seconds=3.0,
        purpose="obtain strict worker proposal envelope",
    )
    executor = LocalProcessExecutor(
        (tmp_path,),
        AtomicArtifactStore(tmp_path / "artifacts"),
        executable_paths=(Path(sys.executable).resolve().parent,),
        stdout_stream_filter_factory=lambda r: model_stdout_filter("codex_cli", r),
    )
    result = executor.execute(request, allow(request.content_digest), NeverCancelled())
    assert result.failure.code.value == "PROCESS_OUTPUT_LIMIT_EXCEEDED"
    assert result.resource_usage["stdout_retained_bytes"] <= 1000
    assert result.resource_usage["stderr_retained_bytes"] <= 1000
    assert result.resource_usage["stdout_raw_limit_bytes"] == 32000
    assert result.resource_usage["process_group_cleanup"] != "not_required"
