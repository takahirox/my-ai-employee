"""Durable parent Job identity for related top-level Graph Runs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .domain.base import Identifier, UtcTimestamp


class JobRecord(BaseModel):
    """The immutable original higher-level goal shared by several Graph Runs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["2"] = "2"
    id: Identifier
    goal: str = Field(min_length=1, max_length=20_000)
    created_at: UtcTimestamp


class JobGraphRunRecord(BaseModel):
    """An ordered relationship from one Job to one independently authoritative Graph Run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["2"] = "2"
    id: Identifier
    job_id: Identifier
    graph_run_id: Identifier
    sequence: int = Field(ge=1)
    created_at: UtcTimestamp
