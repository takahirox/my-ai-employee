from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from ai_employee.domain.v2 import ExecutionResult, WorkerResult
from ai_employee.storage import SQLiteStore
from ai_employee.worker_adapters import CodexCliWorkerAdapter, worker_proposal_schema_json
from tests.test_work_orchestration_v2 import NOW, allow_worker, worker_request


def transport(*, new_file: bool, legacy_ids: bool) -> dict:
    payload = {
        "schema_version": "2",
        "run_id": "untrusted-run",
        "created_at": "2026-01-01T00:00:00Z",
        "paths": ["result.txt"],
        "summary": "Write the requested result.",
    }
    if new_file:
        payload["files"] = [{"path": "result.txt", "content": "after\n"}]
    else:
        payload["unified_diff"] = (
            "diff --git a/result.txt b/result.txt\n"
            "--- a/result.txt\n+++ b/result.txt\n@@ -1 +1 @@\n-before\n+after\n"
        )
    proposal = {
        "schema_version": "2",
        "run_id": "untrusted-run",
        "created_at": "2026-01-01T00:00:00Z",
        "worker_id": "untrusted-worker",
        "kind": "edit_intent",
        "payload": payload,
        "reason": "Supply the requested result.",
        "expected_artifact_kinds": ["workspace_patch"],
    }
    if legacy_ids:
        # Reproduce the digit-leading UUIDs rejected in both benchmark responses.
        proposal["id"] = "8db8984c-b81d-4baa-86b4-aab7c9a0dca4"
        payload["id"] = "78d176bc-b25e-48a5-8094-c5071708598d"
    return {
        "schema_version": "2",
        "proposals": [proposal],
        "assistant_note": "",
        "usage_json": "{}",
        "non_mutating_result": None,
    }


class Executor:
    def execute(self, request, decision, cancellation):
        return ExecutionResult(
            id="execution-test",
            run_id=request.run_id,
            request_digest=request.content_digest,
            created_at=NOW,
            status="succeeded",
            duration_seconds=0.01,
            stdout_artifact_digest="1" * 64,
        )


class Channel:
    def __init__(self):
        self.proposals = []

    def submit(self, proposal):
        self.proposals.append(proposal)


@pytest.mark.parametrize("new_file", [False, True])
@pytest.mark.parametrize("legacy_ids", [False, True])
def test_runtime_ids_reach_mediation_and_survive_persistence(
    tmp_path: Path, new_file: bool, legacy_ids: bool
) -> None:
    output = transport(new_file=new_file, legacy_ids=legacy_ids)
    # Duplicate model IDs must not merge or overwrite independent records.
    output["proposals"].append(deepcopy(output["proposals"][0]))
    adapter = CodexCliWorkerAdapter(
        Executor(), lambda _: json.dumps(output).encode(), allow_worker, run_id="run-1"
    )
    channel = Channel()
    results = [adapter.propose(worker_request(), channel) for _ in range(2)]
    assert all(result.status == "succeeded" for result in results)
    assert len(channel.proposals) == 4
    ids = []
    for proposal in channel.proposals:
        ids.extend((proposal.id, proposal.payload.id))
        assert proposal.id.startswith("proposal-")
        assert proposal.payload.id.startswith("request-")
        assert proposal.run_id == proposal.payload.run_id == "run-1"
        assert proposal.worker_id == "codex_cli"
        assert proposal.payload.paths == ("result.txt",)
        assert "+after\n" in proposal.payload.unified_diff
    assert len(set(ids)) == 8
    with SQLiteStore(tmp_path / "fleet.db") as store:
        for result in results:
            store.put("worker_result_v2", result)
    with SQLiteStore(tmp_path / "fleet.db") as store:
        for result in results:
            restored = store.get("worker_result_v2", result.id, WorkerResult)
            assert restored == result  # IDs, nested digests and originating request unchanged.


@pytest.mark.parametrize("defect", ["paths", "unknown_field", "kind"])
def test_runtime_ids_do_not_accept_invalid_actions(defect: str) -> None:
    output = transport(new_file=False, legacy_ids=True)
    proposal = output["proposals"][0]
    if defect == "paths":
        proposal["payload"]["paths"] = ["different.txt"]
    elif defect == "unknown_field":
        proposal["payload"]["unrestricted"] = True
    else:
        proposal["kind"] = "download"
    channel = Channel()
    result = CodexCliWorkerAdapter(
        Executor(), lambda _: json.dumps(output).encode(), allow_worker, run_id="run-1"
    ).propose(worker_request(), channel)
    assert result.status == "failed"
    assert not channel.proposals


def test_model_schema_omits_runtime_metadata_for_every_proposal_and_request() -> None:
    schema = json.loads(worker_proposal_schema_json())
    for proposal in schema["properties"]["proposals"]["items"]["anyOf"]:
        metadata = {"id", "created_at", "run_id", "worker_id"}
        assert not metadata.intersection(proposal["properties"])
        assert not metadata.intersection(proposal["required"])
        payload = proposal["properties"]["payload"]
        for variant in payload.get("anyOf", [payload]):
            assert not metadata.intersection(variant["properties"])
            assert not metadata.intersection(variant["required"])
            assert variant["additionalProperties"] is False


@pytest.mark.parametrize("legacy_timestamp", [None, "", "not-a-date", "9999-01-01T00:00:00Z"])
def test_runtime_timestamps_replace_legacy_metadata(legacy_timestamp, monkeypatch):
    output = transport(new_file=True, legacy_ids=False)
    proposal = output["proposals"][0]
    for record in (proposal, proposal["payload"]):
        if legacy_timestamp is None:
            record.pop("created_at")
            record.pop("run_id")
        else:
            record["created_at"] = legacy_timestamp
    proposal.pop("worker_id")
    monkeypatch.setattr("ai_employee.worker_adapters.now", lambda: NOW)
    result = CodexCliWorkerAdapter(
        Executor(), lambda _: json.dumps(output).encode(), allow_worker, run_id="run-1"
    ).propose(worker_request(), Channel())
    assert result.status == "succeeded"
    attributed = result.proposals[0]
    assert attributed.created_at == attributed.payload.created_at == NOW
    assert attributed.run_id == attributed.payload.run_id == "run-1"
    assert attributed.worker_id == "codex_cli"
