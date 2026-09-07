from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from ai_employee.process_budget import NodeProcessAdmission
from ai_employee.storage import SQLiteStore


def admission(name: str, *, phase: str = "action", units: int = 1) -> NodeProcessAdmission:
    return NodeProcessAdmission(
        id=name,
        admission_id=name,
        run_id="node",
        node_run_id="node",
        created_at=datetime.now(UTC),
        worker_request_digest="1" * 64,
        service_request_digest="2" * 64,
        phase=phase,
        units=units,  # type: ignore[arg-type]
        process_limit=1,
        native_reservation=0,
        verification_reservation=0,
    )


def test_competing_dispatches_cannot_overspend_and_reopen_does_not_refund(tmp_path: Path) -> None:
    path = tmp_path / "budget.db"
    with SQLiteStore(path):
        pass
    barrier = Barrier(2)

    def claim(name: str) -> bool:
        with SQLiteStore(path) as store:
            barrier.wait(timeout=5)
            return store.claim_node_process(
                admission(name), admission("legacy", phase="legacy", units=0)
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("one", "two")))
    assert sorted(results) == [False, True]
    with SQLiteStore(path) as store:
        # No completion receipt: even an interrupted launch retains its charge.
        assert not store.claim_node_process(
            admission("retry"), admission("legacy", phase="legacy", units=0)
        )
        assert (
            sum(
                item.units
                for item in store.list_records(
                    "node_process_admission_v2", NodeProcessAdmission, run_id="node"
                )
            )
            == 1
        )


def test_legacy_dispatches_are_imported_once_and_request_rebinding_is_rejected(
    tmp_path: Path,
) -> None:
    with SQLiteStore(tmp_path / "legacy.db") as store:
        legacy = admission("legacy", phase="legacy", units=1)
        assert not store.claim_node_process(admission("new"), legacy)
        assert not store.claim_node_process(admission("another"), legacy)
        records = store.list_records(
            "node_process_admission_v2", NodeProcessAdmission, run_id="node"
        )
        assert len(records) == 1
        stale = admission("foreign").model_copy(
            update={"worker_request_digest": "3" * 64, "content_digest": None}
        )
        with pytest.raises(ValueError, match="share one accepted node"):
            store.claim_node_process(stale, legacy)


@pytest.mark.parametrize(
    "field,value", [("node_run_id", "other"), ("admission_id", "other"), ("process_limit", 9)]
)
def test_admission_identity_and_limit_are_digest_bound(field: str, value: object) -> None:
    record = admission("one").model_dump(mode="json")
    record[field] = value
    with pytest.raises(ValueError):
        NodeProcessAdmission.model_validate(record)
