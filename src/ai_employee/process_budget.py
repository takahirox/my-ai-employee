"""Durable admissions for processes dispatched on an accepted node's behalf."""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Literal, Self

from pydantic import Field, model_validator

from .domain.base import Digest, Identifier
from .domain.v2 import DigestedRecordV2, WorkerRequest


class NodeProcessAdmission(DigestedRecordV2):
    schema_name: ClassVar[str] = "node_process_admission"
    node_run_id: Identifier
    worker_request_digest: Digest
    admission_id: Identifier
    phase: Literal["legacy", "action", "verification"]
    service_request_digest: Digest
    units: int = Field(ge=0)
    process_limit: int = Field(ge=0)
    native_reservation: int = Field(ge=0)
    verification_reservation: int = Field(ge=0)

    @model_validator(mode="after")
    def _bound_identity(self) -> Self:
        if self.node_run_id != self.run_id or self.admission_id != self.id:
            raise ValueError("process admission must bind its run and invocation identity")
        if self.phase != "legacy" and self.units != 1:
            raise ValueError("one mediated service dispatch consumes one process admission")
        return self


def accepted_process_limit(request: WorkerRequest) -> int:
    value = (
        request.remaining_budgets.get("processes")
        if isinstance(request.remaining_budgets, Mapping)
        else None
    )
    if type(value) is not int or value < 0:
        raise ValueError("accepted node requires a nonnegative integer process reservation")
    return value
