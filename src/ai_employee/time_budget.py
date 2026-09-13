"""Optional work-policy durations. None means no deadline, never infinity in JSON."""

from __future__ import annotations

import time


def minimum(*seconds: float | None) -> float | None:
    finite = [value for value in seconds if value is not None]
    return min(finite) if finite else None


def remaining(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def exhausted(seconds: float | None) -> bool:
    return seconds is not None and seconds <= 0
