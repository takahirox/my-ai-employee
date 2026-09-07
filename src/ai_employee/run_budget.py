"""Shared active wall-time accounting for one logical run across invocations."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import isfinite
from time import monotonic
from typing import ClassVar, Self
from uuid import uuid4

from pydantic import Field, model_validator

from .domain.base import Digest, Identifier, UtcTimestamp, ensure_utc
from .domain.services_v2 import Cancellation
from .domain.v2 import DigestedRecordV2
from .storage import SQLiteStore


def _utc_now() -> datetime:
    return datetime.now(UTC)


class WallTimeExceeded(BaseException):
    """Runtime control flow, never a model/protocol failure eligible for retry."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


class RunWallStart(DigestedRecordV2):
    schema_name: ClassVar[str] = "run_wall_start"
    invocation_id: Identifier
    graph_run_id: Identifier
    started_at: UtcTimestamp
    legacy_sources: tuple[Digest, ...] = ()
    limit_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _bound_start(self) -> Self:
        if self.graph_run_id != self.run_id or self.started_at != self.created_at:
            raise ValueError("wall interval start must bind its run and clock origin")
        return self


class RunWallFinish(DigestedRecordV2):
    schema_name: ClassVar[str] = "run_wall_finish"
    graph_run_id: Identifier
    start_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    limit_seconds: float = Field(gt=0, allow_inf_nan=False)
    active_seconds: float = Field(ge=0, allow_inf_nan=False)
    recovered_interval: bool = False

    @model_validator(mode="after")
    def _bound_run(self) -> Self:
        if self.graph_run_id != self.run_id:
            raise ValueError("wall interval finish belongs to another run")
        return self


class RunWallBudget:
    def __init__(
        self,
        run_id: str,
        limit: float,
        prior: float,
        started: float,
        clock: Callable[[], float],
    ) -> None:
        self.run_id = run_id
        self.limit = limit
        self.prior = prior
        self.started = started
        self.clock = clock

    @property
    def active_seconds(self) -> float:
        return max(0.0, self.clock() - self.started)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.limit - self.prior - self.active_seconds)

    def check(self) -> None:
        if self.remaining_seconds <= 0:
            raise WallTimeExceeded(self.run_id)


_CURRENT: ContextVar[RunWallBudget | None] = ContextVar("fleet_run_wall_budget", default=None)


def current_wall_budget() -> RunWallBudget | None:
    return _CURRENT.get()


def check_wall_budget() -> None:
    budget = _CURRENT.get()
    if budget is not None:
        budget.check()


def remaining_timeout(configured: float) -> float:
    budget = _CURRENT.get()
    if budget is None:
        return configured
    remaining = budget.remaining_seconds
    if remaining <= 0:
        raise WallTimeExceeded(budget.run_id)
    return min(configured, remaining)


class BudgetCancellation:
    def __init__(self, cancellation: Cancellation) -> None:
        self.cancellation = cancellation

    def cancelled(self) -> bool:
        cancelled = self.cancellation.cancelled()
        budget = _CURRENT.get()
        return cancelled or (budget is not None and budget.remaining_seconds <= 0)


def _import_legacy_usage(
    store: SQLiteStore, run_id: str, limit: float, observed_at: datetime
) -> None:
    from .execution_profile import ExecutionProfile, ProfileTiming
    from .run_ownership import RunExecutionOwnerRecord, RunLeaseClosureRecord

    sources: set[str] = set()
    profile_seconds = 0.0
    try:
        profile = store.get("execution_profile_v2", "profile-" + run_id, ExecutionProfile)
    except KeyError:
        profile = None
    if profile is not None:
        timings = store.list_records("execution_profile_timing_v2", ProfileTiming, run_id=run_id)
        if profile.run_id != run_id or any(
            item.run_id != run_id or item.profile_digest != profile.content_digest
            for item in timings
        ):
            raise ValueError("legacy wall-time profile has stale bindings")
        finished = {
            item.id.removesuffix("-invocation") for item in timings if item.phase == "invocation"
        }
        for item in timings:
            if item.phase == "invocation":
                profile_seconds += item.seconds
            elif (
                item.phase == "invocation_start"
                and item.id.removesuffix("-invocation_start") not in finished
            ):
                profile_seconds += max(
                    item.seconds, item.seconds + (observed_at - item.created_at).total_seconds()
                )
            if item.content_digest is not None:
                sources.add(item.content_digest)
    owners = store.list_records("run_execution_owner_v2", RunExecutionOwnerRecord, run_id=run_id)
    closures = store.list_records("run_lease_closure_v2", RunLeaseClosureRecord, run_id=run_id)
    if any(item.run_id != run_id for item in (*owners, *closures)):
        raise ValueError("legacy wall-time ownership belongs to another run")
    closed = {item.owner_record_digest: item for item in closures}
    owner_seconds = 0.0
    for owner in owners:
        closure = closed.get(owner.content_digest or "")
        end = observed_at if closure is None else closure.closed_at
        owner_seconds += max(0.0, (end - owner.acquired_at).total_seconds())
        sources.add(owner.content_digest or "")
        if closure is not None:
            sources.add(closure.content_digest or "")
    if not sources:
        return
    invocation_id = "run-wall-legacy-" + sha256(run_id.encode()).hexdigest()
    start = RunWallStart(
        id=invocation_id,
        invocation_id=invocation_id,
        run_id=run_id,
        graph_run_id=run_id,
        created_at=observed_at,
        started_at=observed_at,
        limit_seconds=limit,
        legacy_sources=tuple(sorted(sources)),
    )
    store.put_once("run_wall_start_v2", start, run_id=run_id)
    store.put_once(
        "run_wall_finish_v2",
        RunWallFinish(
            id=invocation_id + "-finish",
            run_id=run_id,
            created_at=observed_at,
            graph_run_id=run_id,
            start_digest=start.content_digest or "",
            limit_seconds=limit,
            active_seconds=max(profile_seconds, owner_seconds),
            recovered_interval=True,
        ),
        run_id=run_id,
    )


@contextmanager
def wall_budget_scope(
    store: SQLiteStore,
    run_id: str,
    limit: float,
    *,
    started: float | None = None,
    clock: Callable[[], float] = monotonic,
    utc_clock: Callable[[], datetime] = _utc_now,
) -> Iterator[RunWallBudget]:
    if not isfinite(limit) or limit <= 0:
        raise ValueError("wall-time limit must be positive and finite")
    current = _CURRENT.get()
    if current is not None:
        if current.run_id != run_id:
            raise ValueError("nested wall budget belongs to another logical run")
        current.limit = min(current.limit, limit)
        yield current
        return

    observed_at = utc_clock()
    starts = store.list_records("run_wall_start_v2", RunWallStart, run_id=run_id)
    if not starts:
        _import_legacy_usage(store, run_id, limit, observed_at)
        starts = store.list_records("run_wall_start_v2", RunWallStart, run_id=run_id)
    finishes = store.list_records("run_wall_finish_v2", RunWallFinish, run_id=run_id)
    if any(item.run_id != run_id for item in (*starts, *finishes)):
        raise ValueError("wall-time accounting belongs to another run")
    by_start: dict[str, RunWallFinish] = {}
    for finish in finishes:
        if finish.start_digest in by_start:
            raise ValueError("wall-time history contains duplicate completion receipts")
        by_start[finish.start_digest] = finish
    prior = 0.0
    for record in starts:
        limit = min(limit, record.limit_seconds)
        completed = by_start.pop(record.content_digest or "", None)
        if completed is None:
            # An unclosed interval is charged until recovery, never reset. Once
            # recovered its duration is frozen, so a later pause does not accrue it again.
            elapsed = max(0.0, (observed_at - record.started_at).total_seconds())
            prior += elapsed
            owner = store.current_run_owner(run_id)
            live = (
                owner is not None
                and owner["status"] == "active"
                and observed_at < ensure_utc(owner["expires_at"])
            )
            if not live:
                store.put_once(
                    "run_wall_finish_v2",
                    RunWallFinish(
                        id="run-wall-recovered-" + (record.content_digest or ""),
                        run_id=run_id,
                        created_at=observed_at,
                        graph_run_id=run_id,
                        start_digest=record.content_digest or "",
                        limit_seconds=record.limit_seconds,
                        active_seconds=elapsed,
                        recovered_interval=True,
                    ),
                    run_id=run_id,
                )
        else:
            limit = min(limit, completed.limit_seconds)
            prior += completed.active_seconds
    if by_start:
        raise ValueError("wall-time receipt has no authoritative start")
    started = clock() if started is None else started
    budget = RunWallBudget(run_id, limit, prior, started, clock)
    invocation_id = "run-wall-start-" + uuid4().hex
    began_at = observed_at - timedelta(seconds=budget.active_seconds)
    record = RunWallStart(
        id=invocation_id,
        invocation_id=invocation_id,
        run_id=run_id,
        graph_run_id=run_id,
        created_at=began_at,
        started_at=began_at,
        limit_seconds=limit,
    )
    store.put_once("run_wall_start_v2", record, run_id=run_id)
    token = _CURRENT.set(budget)
    try:
        yield budget
    finally:
        _CURRENT.reset(token)
        finish = RunWallFinish(
            id="run-wall-finish-" + uuid4().hex,
            run_id=run_id,
            created_at=utc_clock(),
            graph_run_id=run_id,
            start_digest=record.content_digest or "",
            limit_seconds=budget.limit,
            active_seconds=budget.active_seconds,
        )
        store.put_once("run_wall_finish_v2", finish, run_id=run_id)
