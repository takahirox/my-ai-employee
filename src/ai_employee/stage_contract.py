"""Request-derived reference contracts and model process boundary checks."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

from .domain.v2 import DecisionOutcome, ExecutionResult, PolicyDecision, ProcessRequest
from .serialization import canonical_json

StageCode = Literal[
    "STAGE_POLICY_BINDING_INVALID",
    "STAGE_PROCESS_BINDING_INVALID",
    "STAGE_REFERENCE_MISMATCH",
    "TASK_REVIEW_REQUEST_BINDING_INVALID",
    "TASK_REVIEW_INVALID_JSON",
    "TASK_REVIEW_INVALID_SCHEMA",
    "TASK_REVIEW_REFERENCE_MISMATCH",
    "TASK_REVIEW_PROCESS_FAILED",
]


class StageContractError(ValueError):
    """A bounded protocol classification, never model-authored diagnostic text."""

    def __init__(self, code: StageCode) -> None:
        self.code = code
        super().__init__(code)


def validate_stage_policy(
    request: ProcessRequest, decision: PolicyDecision, effective_policy_digest: str
) -> None:
    if (
        decision.run_id != request.run_id
        or decision.request_digest != request.content_digest
        or decision.effective_policy_digest != effective_policy_digest
        or decision.outcome is not DecisionOutcome.ALLOW
    ):
        raise StageContractError("STAGE_POLICY_BINDING_INVALID")


def stage_result_matches(request: ProcessRequest, result: ExecutionResult) -> bool:
    return result.run_id == request.run_id and result.request_digest == request.content_digest


def validate_stage_result(request: ProcessRequest, result: ExecutionResult) -> None:
    # Check correlation before reading either stdout or stderr, even on failure.
    if not stage_result_matches(request, result):
        raise StageContractError("STAGE_PROCESS_BINDING_INVALID")


@dataclass(frozen=True)
class ReferenceContract:
    criteria: tuple[str, ...] = ()
    nodes: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("criteria", "nodes", "evidence", "artifacts"):
            object.__setattr__(self, field, tuple(sorted(set(getattr(self, field)))))

    def prompt(self) -> dict[str, object]:
        return {
            "criterion_ids": self.criteria,
            "node_ids": self.nodes,
            "evidence_digests": self.evidence,
            "artifact_digests": self.artifacts,
            "rules": (
                "reviewed_criterion_ids must contain exactly the listed criterion_ids, once each. "
                "reviewed_node_ids must contain exactly the listed node_ids, once each. "
                "Finding references must use only their corresponding listed values. "
                "Do not invent reference IDs or substitute task-local criteria for Goal criteria. "
                "These rules apply only to fields present in the response schema; findings' own "
                "IDs are new labels. Preserve all original requirements in your review; report "
                "material missing evidence using the existing finding/limitation fields, never "
                "by creating new criterion IDs."
            ),
        }

    def schema(self, generic: bytes) -> bytes:
        schema: dict[str, Any] = json.loads(generic)
        refs = {
            "reviewed_criterion_ids": (self.criteria, True),
            "criterion_ids": (self.criteria, False),
            "reviewed_node_ids": (self.nodes, True),
            "node_ids": (self.nodes, False),
            "affected_node_ids": (self.nodes, False),
            "evidence_digests": (self.evidence, False),
            "artifact_digests": (self.artifacts, False),
        }

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for name, prop in value.get("properties", {}).items():
                    if name not in refs:
                        continue
                    allowed, exact = refs[name]
                    if allowed:
                        prop["items"] = {**prop.get("items", {}), "enum": list(allowed)}
                    prop["maxItems"] = min(prop.get("maxItems", len(allowed)), len(allowed))
                    if exact:
                        prop["minItems"] = len(allowed)
                    # Preserve existing minItems (including permitted empty findings).
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(schema)
        return canonical_json(schema).encode()

    def validate(self, payload: dict[str, Any]) -> None:
        allowed = {
            "reviewed_criterion_ids": self.criteria,
            "criterion_ids": self.criteria,
            "reviewed_node_ids": self.nodes,
            "node_ids": self.nodes,
            "affected_node_ids": self.nodes,
            "evidence_digests": self.evidence,
            "artifact_digests": self.artifacts,
        }
        for obj in (payload, *payload.get("findings", ())):
            for name, values in obj.items():
                if name not in allowed:
                    continue
                expected = set(allowed[name])
                if len(values) != len(set(values)) or not set(values) <= expected:
                    raise StageContractError("STAGE_REFERENCE_MISMATCH")
                if name.startswith("reviewed_") and set(values) != expected:
                    raise StageContractError("STAGE_REFERENCE_MISMATCH")


@contextmanager
def schema_argv(argv: tuple[str, ...], schema: bytes) -> Iterator[tuple[str, ...]]:
    """Give Codex an invocation-private file; other transports already carry schema."""
    if "--output-schema" not in argv:
        yield argv
        return
    index = argv.index("--output-schema") + 1
    directory = Path(argv[index]).parent
    with NamedTemporaryFile(prefix="fleet-schema-", suffix=".json", dir=directory) as stream:
        stream.write(schema)
        stream.flush()
        actual = list(argv)
        actual[index] = stream.name
        yield tuple(actual)
