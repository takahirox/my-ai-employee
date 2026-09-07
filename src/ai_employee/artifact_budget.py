"""Durable unique-content admissions for an accepted writing node's artifacts."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, ClassVar, Self

from pydantic import Field, model_validator

from .domain.base import Digest, Identifier
from .domain.v2 import ArtifactDescriptor, DigestedRecordV2, WorkerRequest

if TYPE_CHECKING:
    from .storage import SQLiteStore


class NodeArtifactBudgetExceeded(BaseException):
    """Runtime capacity failure; service adapters must not convert it into success."""


class NodeArtifactAdmission(DigestedRecordV2):
    schema_name: ClassVar[str] = "node_artifact_admission"
    node_run_id: Identifier
    worker_request_digest: Digest
    artifact_digest: Digest
    size_bytes: int = Field(ge=0)
    byte_limit: int = Field(ge=0)

    @model_validator(mode="after")
    def _bound_run(self) -> Self:
        if self.node_run_id != self.run_id:
            raise ValueError("artifact admission belongs to another node run")
        return self


class NodeArtifactBudget:
    def __init__(self, store: SQLiteStore, request: WorkerRequest) -> None:
        value = (
            request.remaining_budgets.get("artifact_bytes")
            if isinstance(request.remaining_budgets, Mapping)
            else None
        )
        if type(value) is not int or value < 0:
            raise ValueError("accepted writing node requires a nonnegative artifact reservation")
        self.store = store
        self.request = request
        self.limit = value

    def charge(self, run_id: str, digest: str, size: int) -> None:
        if run_id != self.request.run_id:
            raise ValueError("artifact producer does not match the accepted node")
        admission = NodeArtifactAdmission(
            id="node-artifact-"
            + sha256(f"{run_id}:{self.request.content_digest}:{digest}".encode()).hexdigest(),
            run_id=run_id,
            created_at=datetime.now(UTC),
            node_run_id=run_id,
            worker_request_digest=self.request.content_digest or "",
            artifact_digest=digest,
            size_bytes=size,
            byte_limit=self.limit,
        )
        if not self.store.claim_node_artifact(admission):
            raise NodeArtifactBudgetExceeded("NODE_ARTIFACT_BUDGET_EXCEEDED")

    def charge_retained(self) -> None:
        for descriptor in self.store.list_records(
            "artifact_descriptor_v2", ArtifactDescriptor, run_id=self.request.run_id
        ):
            self.charge(descriptor.run_id, descriptor.artifact_digest, descriptor.size_bytes)


_CURRENT: ContextVar[NodeArtifactBudget | None] = ContextVar("fleet_node_artifacts", default=None)


def current_artifact_budget() -> NodeArtifactBudget | None:
    return _CURRENT.get()


@contextmanager
def node_artifact_scope(store: SQLiteStore, request: WorkerRequest) -> Iterator[NodeArtifactBudget]:
    budget = NodeArtifactBudget(store, request)
    token = _CURRENT.set(budget)
    try:
        budget.charge_retained()
        yield budget
        # Custom service stores must also satisfy the accepted cumulative limit.
        budget.charge_retained()
    finally:
        _CURRENT.reset(token)
