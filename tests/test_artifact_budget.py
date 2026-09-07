import io
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
from threading import Barrier

import pytest

from ai_employee.artifact_budget import (
    NodeArtifactAdmission,
    NodeArtifactBudgetExceeded,
    node_artifact_scope,
)
from ai_employee.domain.v2 import ArtifactPutRequest, WorkerRequest
from ai_employee.services_v2 import AtomicArtifactStore
from ai_employee.storage import SQLiteStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = "0" * 64


def request(limit=4):
    return WorkerRequest(
        id="worker",
        run_id="node",
        created_at=NOW,
        goal="bounded artifact",
        accepted_plan_digest=ZERO,
        node_id="node-id",
        graph_run_id="graph",
        accepted_graph_revision_digest=ZERO,
        harness_digest=ZERO,
        effective_policy_digest=ZERO,
        remaining_budgets={"artifact_bytes": limit},
    )


def put(artifacts, body):
    return artifacts.put(
        io.BytesIO(body),
        ArtifactPutRequest(
            id="put",
            run_id="node",
            created_at=NOW,
            media_type="text/plain",
            logical_kind="process_stdout",
            producer_action_id="process",
            source="process",
        ),
    )


def test_unique_content_is_charged_before_publication_and_resume_keeps_consumption(tmp_path):
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    database = tmp_path / "budget.db"
    with SQLiteStore(database) as store, node_artifact_scope(store, request()):
        one = put(artifacts, b"abc")
        duplicate = put(artifacts, b"abc")
        assert one.id != duplicate.id
        assert one.artifact_digest == duplicate.artifact_digest
        with pytest.raises(NodeArtifactBudgetExceeded):
            put(artifacts, b"xy")
        assert not (
            artifacts.content_root / sha256(b"xy").hexdigest()[:2] / sha256(b"xy").hexdigest()
        ).exists()
        assert not list(artifacts.temporary_root.iterdir())
    with SQLiteStore(database) as store, node_artifact_scope(store, request()):
        put(artifacts, b"z")
        with pytest.raises(NodeArtifactBudgetExceeded):
            put(artifacts, b"q")
        assert (
            sum(
                r.size_bytes
                for r in store.list_records(
                    "node_artifact_admission_v2", NodeArtifactAdmission, run_id="node"
                )
            )
            == 4
        )


def test_concurrent_producers_cannot_spend_the_same_remaining_bytes(tmp_path):
    database = tmp_path / "concurrent.db"
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    with SQLiteStore(database):
        pass
    barrier = Barrier(2)

    def produce(body):
        with SQLiteStore(database) as store:
            try:
                with node_artifact_scope(store, request()):
                    barrier.wait(5)
                    put(artifacts, body)
            except NodeArtifactBudgetExceeded:
                return False
            return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(produce, (b"abc", b"def"))) == [False, True]
    assert len(list(artifacts.metadata_root.iterdir())) == 1


def test_custom_store_retention_is_checked_and_stale_request_is_rejected(tmp_path):
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    descriptor = put(artifacts, b"12345")
    with (
        SQLiteStore(tmp_path / "custom.db") as store,
        pytest.raises(NodeArtifactBudgetExceeded),
        node_artifact_scope(store, request()),
    ):
        store.put("artifact_descriptor_v2", descriptor, run_id="node")
    with SQLiteStore(tmp_path / "stale.db") as store:
        with node_artifact_scope(store, request()):
            put(artifacts, b"abc")
        with (
            pytest.raises(ValueError, match="stale or duplicate"),
            node_artifact_scope(store, request(10)),
        ):
            put(artifacts, b"z")


@pytest.mark.parametrize(
    "field,value", [("node_run_id", "foreign"), ("byte_limit", 99), ("size_bytes", 0)]
)
def test_admissions_bind_the_node_content_size_and_accepted_limit(field, value):
    record = NodeArtifactAdmission(
        id="admission",
        run_id="node",
        node_run_id="node",
        created_at=NOW,
        worker_request_digest=ZERO,
        artifact_digest="1" * 64,
        byte_limit=4,
        size_bytes=3,
    )
    payload = record.model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValueError):
        NodeArtifactAdmission.model_validate(payload)
