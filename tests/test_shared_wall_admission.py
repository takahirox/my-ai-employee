import pytest
from pydantic import BaseModel

from ai_employee.run_budget import RunWallBudget, WallTimeExceeded
from ai_employee.storage import SQLiteStore


class Reservation(BaseModel):
    id: str
    remaining: dict[str, int | float]


def reserve(store, attempt, seconds, **overrides):
    return store.reserve_graph_node(
        "run",
        "node",
        0,
        attempt,
        max_claims=2,
        worker_turns=1,
        processes=1,
        wall_seconds=180.0,
        artifact_bytes=10,
        limits={"wall_seconds": 180.0, "worker_turns": 2, "processes": 2, "artifact_bytes": 20},
        record_factory=lambda remaining: Reservation(
            id=f"reservation-{attempt}", remaining=remaining
        ),
        wall_budget=(
            None
            if seconds is None
            else RunWallBudget("run", 180.0, 180.0 - seconds, 0.0, lambda: 0.0)
        ),
        **overrides,
    )


def test_serial_repair_uses_remaining_deadline_without_resetting_other_counters(tmp_path):
    path = tmp_path / "state.db"
    with SQLiteStore(path) as store:
        first = reserve(store, 0, 102.0)
        assert first.remaining["wall_seconds"] == 102.0
        assert reserve(store, 0, 102.0) is None
    with SQLiteStore(path) as store:
        repair = reserve(store, 1, 40.0)
        assert repair.remaining["wall_seconds"] == 40.0
        assert repair.remaining["worker_turns"] == 0
        assert repair.remaining["artifact_bytes"] == 0
        assert reserve(store, 2, 39.0) is None


def test_shared_wall_admission_requires_a_live_bounded_remainder(tmp_path):
    with SQLiteStore(tmp_path / "state.db") as store:
        with pytest.raises(WallTimeExceeded):
            reserve(store, 0, 0.0)
        assert reserve(store, 0, 1.0) is not None


def test_no_shared_deadline_preserves_additive_wall_accounting(tmp_path):
    with SQLiteStore(tmp_path / "state.db") as store:
        assert reserve(store, 0, None) is not None
        assert reserve(store, 1, None) is None
