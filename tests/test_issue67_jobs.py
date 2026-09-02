from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_employee.cli import _graph_run_cli_result, build_parser
from ai_employee.inspector import _group_parent_jobs
from ai_employee.inspector_ui import INDEX
from ai_employee.jobs import JobGraphRunRecord, JobRecord
from ai_employee.storage import SQLiteStore


def _summary(run_id: str, status: str, *, attention: bool = False) -> dict[str, object]:
    return {
        "run_id": run_id,
        "repository_id": "repository",
        "repository": "/repository",
        "goal": f"Child {run_id}",
        "status": status,
        "generation": 0,
        "progress": {"completed": 0, "total": 1},
        "active_task": None,
        "active_tasks": [],
        "phase": status,
        "last_updated_at": f"2026-01-0{run_id[-1]}T00:00:00Z",
        "requires_attention": attention,
        "attention": ([{"kind": "run", "condition": status}] if attention else []),
        "attention_count": 1 if attention else 0,
        "attention_available": True,
    }


def test_job_identity_goal_and_order_are_durable_and_atomic(tmp_path) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "fleet.db"
    with SQLiteStore(database) as store:
        first = store.claim_run_id(
            "child-1", tmp_path, job_id="issue-67", job_goal="Implement the larger goal"
        )
        second = store.claim_run_id("child-2", tmp_path, job_id="issue-67")

        assert first is not None and first.sequence == 1
        assert second is not None and second.sequence == 2
        assert store.get("job_v2", "issue-67", JobRecord).goal == "Implement the larger goal"
        assert [
            item.graph_run_id
            for item in sorted(
                store.list_records("job_graph_run_v2", JobGraphRunRecord),
                key=lambda item: item.sequence,
            )
        ] == ["child-1", "child-2"]
        assert store.job_context_for_run("child-2") == {
            "job": store.get("job_v2", "issue-67", JobRecord).model_dump(mode="json"),
            "relationship": second.model_dump(mode="json"),
        }

        with pytest.raises(ValueError, match="different original goal"):
            store.claim_run_id("child-3", tmp_path, job_id="issue-67", job_goal="Replace the goal")
        assert store.repository_for_run("child-3") is None


def test_new_job_requires_original_goal_and_ungrouped_runs_remain_compatible(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with SQLiteStore(tmp_path / "fleet.db") as store:
        with pytest.raises(ValueError, match="supply --job-goal"):
            store.claim_run_id("missing-goal", tmp_path, job_id="new-job")
        assert store.repository_for_run("missing-goal") is None

        assert store.claim_run_id("standalone", tmp_path) is None
        assert store.job_context_for_run("standalone") is None
        assert store.list_records("job_v2", JobRecord) == ()


def test_job_relationship_is_separate_from_graph_revision_ancestry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with SQLiteStore(tmp_path / "fleet.db") as store:
        binding = store.claim_run_id("graph-run", tmp_path, job_id="parent", job_goal="Parent goal")
        assert binding is not None
        assert binding.graph_run_id == "graph-run"
        assert "accepted_graph_revision_digest" not in JobGraphRunRecord.model_fields
        assert "generation" not in JobGraphRunRecord.model_fields
        assert len(store.list_records("job_graph_run_v2", JobGraphRunRecord)) == 1


def test_cli_transports_job_identity_and_help() -> None:
    args = build_parser().parse_args(
        [
            "work",
            "Child goal",
            "--run-id",
            "child-run",
            "--job-id",
            "parent-job",
            "--job-goal",
            "Original parent goal",
        ]
    )
    assert (args.run_id, args.job_id, args.job_goal) == (
        "child-run",
        "parent-job",
        "Original parent goal",
    )
    result = _graph_run_cli_result(
        SimpleNamespace(id="child-run", status="completed", failure_code=None),
        (),
        {"job": {"id": "parent-job"}, "relationship": {"sequence": 2}},
    )
    assert result["job"] == {
        "job": {"id": "parent-job"},
        "relationship": {"sequence": 2},
    }


@pytest.mark.parametrize(
    ("statuses", "active_indexes", "expected", "successful", "terminal"),
    (
        (("running",), (0,), "running", 0, 0),
        (("planned",), (), "planned", 0, 0),
        (("paused",), (), "paused", 0, 0),
        (("waiting_approval",), (), "waiting_approval", 0, 0),
        (("ready_to_promote",), (), "ready_to_promote", 0, 1),
        (("completed",), (), "completed", 1, 1),
        (("failed",), (), "failed", 0, 1),
        (("cancelled",), (), "cancelled", 0, 1),
        (("interrupted",), (), "interrupted", 0, 1),
        (("unrecognized",), (), "unknown", 0, 0),
        (("failed", "completed"), (), "failed", 1, 2),
        (("cancelled", "completed"), (), "cancelled", 1, 2),
        (("interrupted", "completed"), (), "interrupted", 1, 2),
        (("ready_to_promote", "completed"), (), "ready_to_promote", 1, 2),
        (("unknown", "completed"), (), "unknown", 1, 1),
        (("failed", "running"), (1,), "running", 0, 1),
    ),
)
def test_parent_job_single_and_mixed_status_matrix_is_honest(
    tmp_path,
    statuses: tuple[str, ...],
    active_indexes: tuple[int, ...],
    expected: str,
    successful: int,
    terminal: int,
) -> None:  # type: ignore[no-untyped-def]
    with SQLiteStore(tmp_path / "fleet.db") as store:
        for index in range(len(statuses)):
            store.claim_run_id(
                f"child-{index + 1}",
                tmp_path,
                job_id="parent",
                job_goal="Parent goal" if index == 0 else None,
            )
        summaries = [
            _summary(f"child-{index + 1}", status) for index, status in enumerate(statuses)
        ]
        grouped = _group_parent_jobs(
            store,
            [summary for index, summary in enumerate(summaries) if index in active_indexes],
            [summary for index, summary in enumerate(summaries) if index not in active_indexes],
        )
    job = (grouped["active"] or grouped["history"])[0]
    assert job["overall_status"] == expected
    assert job["progress"] == {
        "completed": successful,
        "successful": successful,
        "terminal": terminal,
        "total": len(statuses),
    }


def test_overview_groups_jobs_by_status_and_sequence(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with SQLiteStore(tmp_path / "fleet.db") as store:
        store.claim_run_id("child-1", tmp_path, job_id="parent", job_goal="Parent goal")
        store.claim_run_id("child-2", tmp_path, job_id="parent")

        grouped = _group_parent_jobs(
            store,
            [_summary("child-2", "running")],
            [_summary("child-1", "failed", attention=True)],
        )
        assert grouped["history"] == []
        job = grouped["active"][0]
        assert job["kind"] == "job"
        assert job["goal"] == "Parent goal"
        assert job["overall_status"] == "running"
        assert job["current_status"] == "running"
        assert job["requires_attention"] is True
        assert [item["run_id"] for item in job["child_graph_runs"]] == [
            "child-1",
            "child-2",
        ]
        assert [item["job_sequence"] for item in job["child_graph_runs"]] == [1, 2]

        historical = _group_parent_jobs(
            store,
            [],
            [_summary("child-1", "failed"), _summary("child-2", "completed")],
        )
        assert historical["active"] == []
        assert historical["history"][0]["current_status"] == "completed"
        assert historical["history"][0]["overall_status"] == "failed"


def test_inspector_lists_clickable_job_children() -> None:
    for marker in (
        "Fleet Jobs and standalone runs",
        "child_graph_runs",
        "job-children",
        "Open child Graph Run ",
        "openRun(child.run_id)",
        "child Graph Runs completed successfully",
    ):
        assert marker in INDEX
    assert "method:'POST'" not in INDEX
