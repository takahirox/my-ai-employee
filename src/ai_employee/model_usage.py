"""Body-free provider usage captured at explicitly configured model boundaries."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from typing import ClassVar, Literal

from pydantic import Field

from .domain.base import Digest
from .domain.services_v2 import Cancellation, ProcessExecutor
from .domain.v2 import (
    DigestedRecordV2,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
    WorkerRequest,
)
from .serialization import canonical_digest
from .services_v2._common import identifier, now
from .storage import SQLiteStore


class UsageTotals(DigestedRecordV2):
    schema_name: ClassVar[str] = "model_usage"
    graph_run_id: str
    backend: str
    model: str | None
    effort: str | None
    stage: str
    request_digest: Digest
    configuration_digest: Digest
    prompt_digest: Digest | None = None
    status: str
    complete: bool = False
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cost_kind: Literal["unavailable", "cli_estimate"] = "unavailable"
    cost_basis: str | None = None
    usage_source: Literal["unavailable", "codex_turn_completed", "claude_result"] = "unavailable"
    scope: Literal["invocation", "main_cli_loop"] = "invocation"


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _sum_known(values: Iterable[int | float | None]) -> int | float | None:
    items = tuple(values)
    if not items or any(value is None for value in items):
        return None
    total = sum(value for value in items if value is not None)
    return None if isinstance(total, float) and not math.isfinite(total) else total


def codex_events(output: str) -> tuple[dict[str, object], ...]:
    events = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            events.append(event)
    return tuple(events)


def codex_payload(output: str) -> str:
    events = codex_events(output)
    if not events:
        # Retain compatibility with older CLI final-message transports, not their usage claims.
        return output.strip()
    if any(event["type"] in {"turn.failed", "error"} for event in events):
        raise ValueError("Codex reported a terminal failure")
    if not events or events[-1]["type"] != "turn.completed":
        raise ValueError("Codex JSONL did not complete its turn")
    messages = [
        item["text"]
        for event in events
        if event["type"] == "item.completed"
        and isinstance(item := event.get("item"), dict)
        and item.get("type") == "agent_message"
        and isinstance(item.get("text"), str)
    ]
    if not messages:
        raise ValueError("Codex JSONL lacks a final agent message")
    return str(messages[-1])


def provider_usage(backend: str, output: str) -> dict[str, object]:
    if backend == "codex_cli":
        samples = [
            sample
            for event in codex_events(output)
            if event["type"] == "turn.completed" and isinstance(sample := event.get("usage"), dict)
        ]
        if not samples:
            return {}
        values: dict[str, object] = {"usage_source": "codex_turn_completed"}
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ):
            values[key] = _sum_known(_count(sample.get(key)) for sample in samples)
        if (
            isinstance(values["input_tokens"], int)
            and isinstance(values["cached_input_tokens"], int)
            and values["cached_input_tokens"] > values["input_tokens"]
        ):
            values["cached_input_tokens"] = None
        return values
    if backend != "claude_code_cli":
        return {}
    try:
        wrapper = json.loads(output)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(wrapper, dict) or wrapper.get("type") != "result":
        return {}
    usage = wrapper.get("usage")
    values = {"usage_source": "claude_result", "scope": "main_cli_loop"}
    if isinstance(usage, dict):
        uncached = _count(usage.get("input_tokens"))
        cached = _count(usage.get("cache_read_input_tokens"))
        written = _count(usage.get("cache_creation_input_tokens"))
        values.update(
            input_tokens=_sum_known((uncached, cached, written)),
            cached_input_tokens=cached,
            cache_write_tokens=written,
            output_tokens=_count(usage.get("output_tokens")),
        )
    cost = wrapper.get("total_cost_usd")
    if (
        isinstance(cost, (float, int))
        and not isinstance(cost, bool)
        and math.isfinite(cost)
        and cost >= 0
    ):
        values.update(
            cost_usd=cost,
            cost_kind="cli_estimate",
            cost_basis="CLI-reported invocation estimate; bundled pricing version unavailable; "
            "may include CLI subagents excluded from main-loop tokens; not billed cost",
        )
    return values


_MODEL_STAGES = {
    "obtain strict worker proposal envelope": "worker",
    "obtain strict repository-isolated semantic task assessment": "assessment",
    "obtain a strict non-authoritative ProposedGraph": "planning",
    "obtain one strict non-authoritative ProposedGraph revision": "plan_revision",
    "obtain a strict non-authoritative PlanReviewPayload": "plan_review",
    "obtain a strict non-authoritative TaskReviewPayload": "task_review",
    "obtain strict non-authoritative parent semantic evidence": "goal_review",
}


def filter_model_stdout(backend: str, request: ProcessRequest, data: bytes) -> bytes:
    """Retain the existing final-payload contract and numeric accounting, not tool traces."""

    if request.purpose not in _MODEL_STAGES or backend != "codex_cli":
        return data
    output = data.decode("utf-8", "replace")
    events = codex_events(output)
    if not events:
        return data
    selected: list[dict[str, object]] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            selected.append({"type": "error"})
            break
        if not isinstance(event, dict):
            selected.append({"type": "error"})
            break
        kind = event.get("type")
        if kind == "item.completed" and isinstance(item := event.get("item"), dict):
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                selected = [entry for entry in selected if entry.get("type") != "item.completed"]
                selected.append(
                    {"type": kind, "item": {"type": "agent_message", "text": item["text"]}}
                )
        elif kind == "turn.completed":
            usage = event.get("usage")
            selected.append(
                {
                    "type": kind,
                    "usage": {
                        key: _count(usage.get(key))
                        for key in (
                            "input_tokens",
                            "cached_input_tokens",
                            "cache_write_tokens",
                            "output_tokens",
                            "reasoning_output_tokens",
                        )
                    }
                    if isinstance(usage, dict)
                    else {},
                }
            )
        elif kind in {"error", "turn.failed", "turn.started"}:
            selected.append({"type": kind})
    return "\n".join(
        json.dumps(event, separators=(",", ":"), ensure_ascii=False) for event in selected
    ).encode()


class UsageRecordingExecutor:
    """Observe trusted model invocations only; return the original execution result."""

    def __init__(
        self,
        executor: ProcessExecutor,
        store: SQLiteStore,
        output_reader: Callable[[str], bytes],
        *,
        graph_run_id: str,
        backend: str,
        model: str | None,
        effort: str | None,
        configuration_digest: str,
    ) -> None:
        self.executor, self.store, self.output_reader = executor, store, output_reader
        self.graph_run_id, self.backend, self.model, self.effort = (
            graph_run_id,
            backend,
            model,
            effort,
        )
        self.configuration_digest = configuration_digest

    def execute(
        self,
        request: ProcessRequest,
        decision: PolicyDecision,
        cancellation: Cancellation,
    ) -> ExecutionResult:
        result = self.executor.execute(request, decision, cancellation)
        stage = _MODEL_STAGES.get(request.purpose)
        if stage is None or decision.outcome.value != "allow":
            return result
        if result.run_id != request.run_id or result.request_digest != request.content_digest:
            return result
        output = ""
        if result.stdout_artifact_digest:
            with suppress(OSError, ValueError, KeyError):
                output = self.output_reader(result.stdout_artifact_digest).decode(
                    "utf-8", "replace"
                )
        record = UsageTotals.model_validate(
            {
                "id": "usage-" + canonical_digest((request.id, result.id)),
                "run_id": request.run_id,
                "created_at": now(),
                "graph_run_id": self.graph_run_id,
                "backend": self.backend,
                "model": self.model,
                "effort": self.effort,
                "stage": stage,
                "request_digest": request.content_digest or "",
                "prompt_digest": request.stdin_artifact_digest,
                "configuration_digest": self.configuration_digest,
                "status": result.status,
                "complete": result.status == "succeeded"
                and _transport_complete(self.backend, output),
                "duration_seconds": result.duration_seconds,
                **provider_usage(self.backend, output),
            }
        )
        self.store.put_once("model_usage_v2", record, run_id=self.graph_run_id)
        return result


def summarize_usage(records: Iterable[UsageTotals]) -> dict[str, object]:
    unique = {record.id: record for record in records}
    items = tuple(unique.values())
    metrics: dict[str, object] = {}
    for field in (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "cost_usd",
        "duration_seconds",
    ):
        values = tuple(getattr(item, field) for item in items)
        complete_values = tuple(
            value if item.complete else None for item, value in zip(items, values, strict=True)
        )
        metrics[field] = {
            "total": _sum_known(complete_values),
            "known_subtotal": _sum_known(value for value in values if value is not None),
            "reported_invocations": sum(value is not None for value in values),
        }
    inputs = _sum_known(item.input_tokens if item.complete else None for item in items)
    cached = _sum_known(item.cached_input_tokens if item.complete else None for item in items)
    return {
        "invocations": len(items),
        "metrics": metrics,
        "cache_hit_ratio": cached / inputs if inputs and cached is not None else None,
        "uncached_input_tokens": inputs - cached
        if inputs is not None and cached is not None
        else None,
        "cost_kind": "cli_estimate"
        if items and all(item.cost_kind == "cli_estimate" for item in items)
        else "unavailable_or_partial",
        "by_stage": {
            stage: summarize_usage_group(item for item in items if item.stage == stage)
            for stage in sorted({item.stage for item in items})
        },
        "invocation_details": [
            item.model_dump(mode="json") for item in sorted(items, key=lambda item: item.id)
        ],
    }


def _transport_complete(backend: str, output: str) -> bool:
    if backend == "codex_cli":
        events = codex_events(output)
        return (
            bool(events)
            and events[-1]["type"] == "turn.completed"
            and not any(event["type"] in {"turn.failed", "error"} for event in events)
        )
    if backend == "claude_code_cli":
        try:
            result = json.loads(output)
        except (ValueError, RecursionError):
            return False
        return (
            isinstance(result, dict)
            and result.get("type") == "result"
            and result.get("is_error") is False
        )
    return False


def summarize_usage_group(records: Iterable[UsageTotals]) -> dict[str, object]:
    items = tuple(records)
    return {
        "invocations": len(items),
        **{
            field: _sum_known(getattr(item, field) if item.complete else None for item in items)
            for field in ("input_tokens", "cached_input_tokens", "output_tokens", "cost_usd")
        },
    }


def inspect_usage(store: SQLiteStore, run_ids: Iterable[str]) -> dict[str, object]:
    return summarize_usage(
        item
        for run_id in sorted(set(run_ids))
        for item in store.list_records("model_usage_v2", UsageTotals, run_id=run_id)
    )


def record_native_usage(
    store: SQLiteStore,
    graph_run_id: str,
    configuration_digest: str,
    model: str,
    effort: str,
    request: WorkerRequest,
    usage: Mapping[str, object],
    duration: float,
    status: str,
) -> None:
    metrics = (
        provider_usage("codex_cli", json.dumps({"type": "turn.completed", "usage": dict(usage)}))
        if usage
        else {}
    )
    record = UsageTotals.model_validate(
        {
            "id": identifier("native-usage"),
            "run_id": request.run_id,
            "created_at": now(),
            "graph_run_id": graph_run_id,
            "backend": "codex_cli",
            "model": model,
            "effort": effort,
            "stage": "worker",
            "request_digest": request.content_digest,
            "configuration_digest": configuration_digest,
            "status": status,
            "complete": status == "succeeded",
            "duration_seconds": duration,
            **metrics,
        }
    )
    store.put_once("model_usage_v2", record, run_id=graph_run_id)
