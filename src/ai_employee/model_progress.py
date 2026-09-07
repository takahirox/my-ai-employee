"""Bounded, body-free observations of a model CLI's live event stream."""

from __future__ import annotations

import json
from collections.abc import Callable
from hashlib import sha256
from typing import Literal

from pydantic import Field

from .domain.base import Digest
from .domain.v2 import DigestedRecordV2, ProcessRequest
from .services_v2._common import now
from .storage import SQLiteStore


class ModelProgressRecord(DigestedRecordV2):
    schema_name = "model_progress"
    graph_run_id: str
    request_digest: Digest
    event: Literal[
        "turn.started", "turn.completed", "turn.failed", "error", "item.started", "item.completed"
    ]
    item_type: (
        Literal[
            "command_execution",
            "file_change",
            "mcp_tool_call",
            "web_search",
            "agent_message",
            "reasoning",
            "other",
        ]
        | None
    ) = None
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    item_duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    exit_code: int | None = None
    events_observed: int = Field(ge=1)
    history_truncated: bool = False


def progress_observer(
    store: SQLiteStore, graph_run_id: str, request: ProcessRequest
) -> Callable[[bytes, float], None]:
    pending = b""
    discarding = False
    count = 0
    starts: dict[str, float] = {}
    identity = sha256(request.id.encode()).hexdigest()[:24]
    event_names = {
        "turn.started",
        "turn.completed",
        "turn.failed",
        "error",
        "item.started",
        "item.completed",
    }
    item_names = {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search",
        "agent_message",
        "reasoning",
    }

    def observe(chunk: bytes, elapsed: float) -> None:
        nonlocal pending, discarding, count
        lines = (pending + chunk).split(b"\n")
        pending = lines.pop()
        latest: ModelProgressRecord | None = None
        for line in lines:
            if discarding:
                discarding = False
                continue
            if len(line) > 65536:
                continue
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("type"), str)
                or event["type"] not in event_names
            ):
                continue
            kind = event["type"]
            item = event.get("item")
            item = item if isinstance(item, dict) else {}
            item_type = item.get("type")
            item_type = (
                item_type if isinstance(item_type, str) and item_type in item_names else "other"
            )
            item_id = item.get("id")
            duration = None
            if isinstance(item_id, str) and len(item_id) <= 256:
                if kind == "item.started" and len(starts) < 128:
                    starts[item_id] = elapsed
                elif kind == "item.completed":
                    started = starts.pop(item_id, None)
                    if started is not None:
                        duration = max(0.0, elapsed - started)
            exit_code = item.get("exit_code")
            if type(exit_code) is not int or not -(2**31) <= exit_code < 2**31:
                exit_code = None
            count += 1
            # Retain a bounded prefix plus the most recent event. No item IDs,
            # command strings, arguments, provider timestamps, or output bodies.
            record = ModelProgressRecord.model_validate(
                {
                    "id": f"progress-{identity}-{min(count, 128)}",
                    "run_id": request.run_id,
                    "created_at": now(),
                    "graph_run_id": graph_run_id,
                    "request_digest": request.content_digest,
                    "event": kind,
                    "item_type": item_type if kind.startswith("item.") else None,
                    "elapsed_seconds": elapsed,
                    "item_duration_seconds": duration,
                    "exit_code": exit_code,
                    "events_observed": count,
                    "history_truncated": count > 128,
                }
            )
            if count < 128:
                store.put("model_progress_v2", record, run_id=graph_run_id)
            else:
                latest = record
        if latest is not None:
            store.put("model_progress_v2", latest, run_id=graph_run_id)
        if len(pending) > 65536:
            pending = b""
            discarding = True

    return observe
