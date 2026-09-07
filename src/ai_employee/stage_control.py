"""Dynamically scoped control for synchronous model and verification stages.

Adapters poll this token through their ProcessExecutor. The owning runtime retains
authority over heartbeat and stop decisions; adapters never receive that authority.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .domain.services_v2 import Cancellation
from .run_budget import current_wall_budget

_CURRENT: ContextVar[Cancellation | None] = ContextVar("fleet_stage_cancellation", default=None)


@contextmanager
def bind_stage_cancellation(cancellation: Cancellation) -> Iterator[None]:
    token = _CURRENT.set(cancellation)
    try:
        yield
    finally:
        _CURRENT.reset(token)


class StageCancellation:
    """Resolve the active runtime token without retaining a previous invocation."""

    def cancelled(self) -> bool:
        current = _CURRENT.get()
        cancelled = current is not None and current.cancelled()
        budget = current_wall_budget()
        return cancelled or (budget is not None and budget.remaining_seconds <= 0)
