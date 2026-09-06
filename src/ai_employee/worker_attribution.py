"""Versioned model content, attributed only by the originating invocation."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .domain.base import DIGEST_PATTERN
from .domain.v2 import NonMutatingResult, WorkerRequest
from .serialization import canonical_json
from .services_v2._common import identifier, now


class ModelReadOnlyResult(BaseModel):
    """Wire v3 deliberately contains no runtime identity or authority fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["3"]
    logical_kind: Literal["diagnosis", "research"]
    media_type: Literal["text/plain", "text/markdown"]
    content: str
    summary: str | None
    findings: tuple[str, ...]
    evidence_refs: tuple[str, ...]


def attribute_read_only_payload(payload: str, request: WorkerRequest) -> str:
    """Convert new wire content; never relabel a legacy bound envelope.

    Called only after transport correlation has been checked. WorkerRequest is an
    immutable originating snapshot, not the graph's mutable current-node state.
    The existing authoritative result model still enforces substantive constraints.
    """
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        return payload
    result = raw.get("non_mutating_result")
    if not isinstance(result, dict) or result.get("schema_version") != "3":
        return payload
    if request.task_kind.value != "non_mutating" or raw.get("proposals"):
        raise ValueError("read-only wire v3 cannot authorize mutations or proposals")
    if any(
        value is None
        for value in (
            request.graph_run_id,
            request.node_id,
            request.accepted_graph_revision_digest,
            request.content_digest,
        )
    ):
        raise ValueError("read-only wire v3 requires a complete invocation binding")
    content = ModelReadOnlyResult.model_validate_json(json.dumps(result), strict=True)
    attributed = NonMutatingResult(
        id=identifier("typed-result"),
        run_id=request.run_id,
        created_at=now(),
        graph_run_id=request.graph_run_id,
        worker_request_digest=request.content_digest,
        node_id=request.node_id,
        accepted_graph_revision_digest=request.accepted_graph_revision_digest,
        generation=request.generation,
        attempt=request.attempt,
        **content.model_dump(exclude={"schema_version"}),
    )
    raw["non_mutating_result"] = attributed.model_dump(mode="json")
    return canonical_json(raw)


def model_read_only_schema() -> dict[str, object]:
    schema = ModelReadOnlyResult.model_json_schema()
    # Copy the authoritative content bounds, without exposing authoritative identity.
    authoritative = NonMutatingResult.model_json_schema()["properties"]
    properties = schema["properties"]
    for name in ("logical_kind", "media_type", "content", "summary", "findings", "evidence_refs"):
        properties[name] = authoritative[name]
    properties["evidence_refs"] = {
        "type": "array",
        "maxItems": 64,
        "items": {"type": "string", "pattern": DIGEST_PATTERN},
    }
    return schema
