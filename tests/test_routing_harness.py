from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_employee.domain import HarnessWorker, ProjectHarnessV2


def test_worker_routing_defaults_are_restrictive() -> None:
    worker = HarnessWorker()
    assert worker.allowed_strategy_ids == ()
    assert worker.adaptive_routing is False
    assert worker.local_backend is False


def test_worker_accepts_explicit_routing_values() -> None:
    worker = HarnessWorker(
        allowed_strategy_ids=("fast", "safe"),
        adaptive_routing=True,
        local_backend=True,
    )
    assert worker.allowed_strategy_ids == ("fast", "safe")
    assert worker.adaptive_routing is True
    assert worker.local_backend is True


@pytest.mark.parametrize(
    "strategy_ids",
    [
        ("fast", "fast"),
        ("",),
        (" ",),
    ],
)
def test_worker_rejects_duplicate_or_blank_strategy_ids(
    strategy_ids: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError, match=r"unique|non-blank"):
        HarnessWorker(allowed_strategy_ids=strategy_ids)


@pytest.mark.parametrize(
    "worker",
    [
        HarnessWorker(allowed_strategy_ids=("fast",)),
        HarnessWorker(adaptive_routing=True),
        HarnessWorker(local_backend=True),
    ],
)
def test_provisional_harness_denies_routing_authority(worker: HarnessWorker) -> None:
    with pytest.raises(ValidationError, match="worker authority"):
        ProjectHarnessV2(provisional=True, worker=worker)
