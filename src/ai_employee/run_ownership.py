"""Durable, generation-fenced ownership facts for top-level graph execution."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal, Self

from pydantic import Field, model_validator

from .domain.base import Digest, Identifier, UtcTimestamp
from .domain.v2 import DigestedRecordV2


class RunExecutionOwnerRecord(DigestedRecordV2):
    """Immutable acquisition authority for one exact graph execution attempt."""

    schema_name: ClassVar[str] = "run_execution_owner_record"
    graph_run_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    execution_attempt: int = Field(ge=0)
    owner_instance_id: Identifier
    acquired_at: UtcTimestamp
    last_heartbeat_at: UtcTimestamp
    expires_at: UtcTimestamp
    lease_duration_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _valid_acquisition(self) -> Self:
        if self.run_id != self.graph_run_id:
            raise ValueError("run owner must be stored under its graph run ID")
        if self.acquired_at != self.created_at:
            raise ValueError("run owner acquisition and creation timestamps must match")
        if self.last_heartbeat_at != self.acquired_at:
            raise ValueError("new run owner heartbeat must begin at acquisition")
        if self.expires_at <= self.last_heartbeat_at:
            raise ValueError("run owner lease must expire after its heartbeat")
        return self


class RunLeaseHeartbeatRecord(DigestedRecordV2):
    """Immutable renewal fact chained to the authoritative acquisition."""

    schema_name: ClassVar[str] = "run_lease_heartbeat_record"
    graph_run_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    execution_attempt: int = Field(ge=0)
    owner_instance_id: Identifier
    owner_record_id: Identifier
    owner_record_digest: Digest
    previous_heartbeat_digest: Digest
    heartbeat_at: UtcTimestamp
    expires_at: UtcTimestamp

    @model_validator(mode="after")
    def _valid_heartbeat(self) -> Self:
        if self.run_id != self.graph_run_id:
            raise ValueError("run heartbeat must be stored under its graph run ID")
        if self.heartbeat_at != self.created_at:
            raise ValueError("run heartbeat and creation timestamps must match")
        if self.expires_at <= self.heartbeat_at:
            raise ValueError("renewed lease must expire after its heartbeat")
        return self


class RunLeaseClosureRecord(DigestedRecordV2):
    """Immutable proof that an authoritative owner closed its lease."""

    schema_name: ClassVar[str] = "run_lease_closure_record"
    graph_run_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    execution_attempt: int = Field(ge=0)
    owner_instance_id: Identifier
    owner_record_id: Identifier
    owner_record_digest: Digest
    final_heartbeat_digest: Digest
    closed_at: UtcTimestamp
    terminal_graph_status: Literal["cancelled", "completed", "failed", "interrupted", "paused"]
    reason: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _valid_closure(self) -> Self:
        if self.run_id != self.graph_run_id:
            raise ValueError("run closure must be stored under its graph run ID")
        if self.closed_at != self.created_at:
            raise ValueError("run closure and creation timestamps must match")
        return self


class RunOwnerConflictRecord(DigestedRecordV2):
    """Persisted evidence that a second owner failed to acquire live authority."""

    schema_name: ClassVar[str] = "run_owner_conflict_record"
    graph_run_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    execution_attempt: int = Field(ge=0)
    rejected_owner_instance_id: Identifier
    current_owner_record_id: Identifier
    current_owner_record_digest: Digest
    current_owner_instance_id: Identifier
    current_generation: int = Field(ge=0)
    current_execution_attempt: int = Field(ge=0)
    last_heartbeat_at: UtcTimestamp
    expires_at: UtcTimestamp


class OwnerFenceViolationRecord(DigestedRecordV2):
    """Read-only diagnostic evidence for a rejected stale-owner operation."""

    schema_name: ClassVar[str] = "owner_fence_violation_record"
    graph_run_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    execution_attempt: int = Field(ge=0)
    owner_instance_id: Identifier
    owner_record_id: Identifier
    owner_record_digest: Digest
    operation: Literal["heartbeat", "terminalize", "write", "consume_child_result"]
    observed_at: UtcTimestamp
    reason: Literal["expired", "closed", "missing", "stale", "superseded"]


class RunOrphanRecoveryRecord(DigestedRecordV2):
    """Explicit idempotent terminalization of one exact expired owner generation."""

    schema_name: ClassVar[str] = "run_orphan_recovery_record"
    graph_run_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    execution_attempt: int = Field(ge=0)
    expired_owner_record_id: Identifier
    expired_owner_record_digest: Digest
    last_heartbeat_at: UtcTimestamp
    expired_at: UtcTimestamp
    recovered_at: UtcTimestamp
    terminal_graph_status: Literal["interrupted"] = "interrupted"

    @model_validator(mode="after")
    def _valid_recovery(self) -> Self:
        if self.run_id != self.graph_run_id:
            raise ValueError("run recovery must be stored under its graph run ID")
        if self.recovered_at != self.created_at:
            raise ValueError("run recovery and creation timestamps must match")
        if self.recovered_at < self.expired_at:
            raise ValueError("a live run owner cannot be recovered")
        return self


def lease_is_expired(expires_at: datetime, observed_at: datetime) -> bool:
    """Use an inclusive boundary so an owner cannot renew at its expiry instant."""

    return observed_at >= expires_at
