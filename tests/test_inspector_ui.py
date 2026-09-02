from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from ai_employee.domain import Node, NodeKind, OutputContract
from ai_employee.domain.models import NodeResourceBudget
from ai_employee.inspector import _INDEX, _node_execution_projection, inspect_fleet_runs
from ai_employee.serialization import canonical_json
from ai_employee.task_orchestration import NodeExecutionRecord


class _InspectorDocument(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.tabs: set[str] = set()
        self.buttons: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if element_id := attributes.get("id"):
            self.ids.add(element_id)
        if tab := attributes.get("data-tab"):
            self.tabs.add(tab)
        if tag == "button" and (button_id := attributes.get("id")):
            self.buttons.add(button_id)


def test_inspector_ui_exposes_read_only_dag_and_task_detail_contract() -> None:
    document = _InspectorDocument()
    document.feed(_INDEX)
    assert document.ids >= {
        "active-runs",
        "active-count",
        "back-to-fleet",
        "fleet-overview",
        "history-runs",
        "history-count",
        "history-section",
        "app",
        "connection-status",
        "details",
        "explanation",
        "graph",
        "raw",
        "repository-filter",
        "revision",
        "revision-story",
        "summary",
    }
    assert document.tabs == {"dag", "raw", "explanation"}
    assert document.buttons == {"back-to-fleet", "refresh"}

    for marker in (
        "Fleet runs",
        "Active",
        "History",
        "loadOverview",
        "Graph revision",
        "Raw Inspector record",
        "Run explanation record",
        "taskView",
        "renderGraph",
        "Repository filter",
        "Task Summary",
        "Current / Recent Activity",
        "Deadline, cancellation, and Fleet Doctor",
        "Typed result acceptances",
        "Live",
        "Reconnecting",
        "Disconnected",
    ):
        assert marker in _INDEX

    for state in (
        "ready",
        "waiting",
        "routed",
        "running",
        "overdue",
        "passed",
        "failed",
        "blocked",
        "cancelled",
        "retained",
    ):
        assert f".state-{state}" in _INDEX

    assert "method:'GET'" in _INDEX
    assert "method:'POST'" not in _INDEX
    assert "method:'PUT'" not in _INDEX
    assert "method:'DELETE'" not in _INDEX
    assert "getJSON('/api/overview'" in _INDEX
    assert "$('#run')" not in _INDEX
    assert "#run-list" not in _INDEX
    assert "new EventSource('/api/events')" in _INDEX
    assert "eventSource.addEventListener('freshness'" in _INDEX
    assert "eventSource.onerror" in _INDEX
    assert "eventSource.close()" in _INDEX
    assert "await refreshRunCatalog()" in _INDEX
    assert "await load(true)" in _INDEX
    assert "selectedRevision=preserve&&" in _INDEX
    assert "!revisionTasks().some(x=>x.id===selectedTask)" in _INDEX
    assert "selectTab(selectedTab)" in _INDEX
    assert "succeeded:'completed'" in _INDEX
    assert ".filter(x=>x.target_id===id).map(x=>x.source_id)" in _INDEX
    assert "Current unaccepted proposal" in _INDEX
    assert "story.graph?.accepted===true" in _INDEX
    assert "removed_task_summaries" in _INDEX
    assert "stateReasons" in _INDEX
    assert "-webkit-line-clamp:2" in _INDEX
    assert "minmax(min(100%,280px),1fr)" in _INDEX
    assert "title.title=run.goal||run.run_id" in _INDEX
    assert "repository.title=repositoryText" in _INDEX
    assert "className='badge run-status state-'" in _INDEX
    assert "progress.setAttribute(" in _INDEX
    assert "'aria-label'," in _INDEX
    assert "const taskOrPhase=run.active_task||run.phase||" in _INDEX
    assert "attention.title=attentionConditions.length?" in _INDEX
    assert "attentionCount+' warning'" in _INDEX
    assert 'id="warning-summary"' in _INDEX
    assert "function renderWarningSummary(){" in _INDEX
    assert "const warnings=maps(raw.attention);" in _INDEX
    assert "button.dataset.taskId=task.id" in _INDEX
    assert "Persisted attention facts were not recorded" in _INDEX
    assert "Open run explanation" in _INDEX
    assert "['Active task',run.active_task]" not in _INDEX
    assert "['Phase',run.phase]" not in _INDEX
    assert "['Attention',run.attention.length?" not in _INDEX
    assert "updated.dataset.timestamp=run.last_updated_at" in _INDEX
    assert "updated.title='Last updated at '+run.last_updated_at" in _INDEX
    assert "'Last updated at '+run.last_updated_at" in _INDEX
    assert "updated.textContent='Updated: Not recorded'" in _INDEX
    assert "relativeTime(updated.dataset.timestamp," in _INDEX
    assert "window.setInterval(" in _INDEX
    assert "refreshRelativeTimes," in _INDEX
    assert "window.clearInterval(relativeTimeTimer)" in _INDEX
    for marker in (
        "cardFacts",
        "operational_status",
        "selected_strategy_id",
        "running_started_at",
        "last_persisted_activity_at",
        "finished_at",
        "elapsed_seconds",
        "wall_time_budget_seconds",
        "deadline_at",
        "verification_count",
        "state-overdue",
        "aria-label",
        "taskActivities",
        "renderTaskSummary",
        "renderTaskActivity",
        "worker_timeout_authorities",
        "node_watchdogs",
        "node_control_propagations",
        "@media(max-width:600px)",
    ):
        assert marker in _INDEX
    card_source = _INDEX[_INDEX.index("function cardFacts") : _INDEX.index("function renderGraph")]
    assert "task.objective" not in card_source
    assert "content_digest" not in card_source
    assert "routing_reasons" not in card_source
    activity_source = _INDEX[
        _INDEX.index("function taskActivities") : _INDEX.index("function taskView")
    ]
    for persisted_source in (
        "node_history",
        "worker_results",
        "artifact_descriptors",
        "typed_result_acceptances",
        "node_evidence",
        "node_evaluator_decisions",
        "worker_boundary_diagnostics",
        "loop_transitions",
    ):
        assert persisted_source in _INDEX
    assert "assistant_note" not in activity_source
    details_source = _INDEX[_INDEX.index("function renderDetails") : _INDEX.index("function add(")]
    assert details_source.index("renderTaskSummary") < details_source.index("'Operational facts'")
    assert details_source.index("renderTaskActivity") < details_source.index("'Operational facts'")


def _execution_record(
    *,
    record_id: str,
    sequence: int,
    transitioned_at: datetime,
    status: str,
) -> NodeExecutionRecord:
    return NodeExecutionRecord.model_validate(
        {
            "id": record_id,
            "run_id": "timed-run",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "transitioned_at": transitioned_at,
            "node_id": "timed-node",
            "accepted_graph_revision_digest": "a" * 64,
            "generation": 0,
            "attempt": 2,
            "sequence": sequence,
            "status": status,
        },
        strict=True,
    )


def test_node_transition_timestamp_serializes_and_drives_overdue_projection() -> None:
    started_at = datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)
    pending = _execution_record(
        record_id="execution-pending",
        sequence=0,
        transitioned_at=started_at - timedelta(seconds=5),
        status="pending",
    )
    running = _execution_record(
        record_id="execution-running",
        sequence=1,
        transitioned_at=started_at,
        status="running",
    )
    node = Node(
        id="timed-node",
        kind=NodeKind.FUNCTION,
        name="Timed node",
        output_contract=OutputContract(id="timed-output"),
        resource_budget=NodeResourceBudget(wall_seconds=10.0),
    )

    projection = _node_execution_projection(
        running,
        (pending, running),
        node,
        None,
        None,
        started_at + timedelta(seconds=10),
    )

    assert projection["operational_status"] == "overdue"
    assert projection["running_started_at"] == "2026-01-01T00:00:05.000000Z"
    assert projection["last_persisted_activity_at"] == "2026-01-01T00:00:05.000000Z"
    assert projection["finished_at"] is None
    assert projection["elapsed_seconds"] == 10.0
    assert projection["deadline_at"] == "2026-01-01T00:00:15.000000Z"
    assert projection["verification_count"] == 0
    assert NodeExecutionRecord.model_validate_json(canonical_json(running), strict=True) == running

    payload = running.model_dump(mode="python")
    payload.pop("transitioned_at")
    payload["content_digest"] = None
    with pytest.raises(ValidationError):
        NodeExecutionRecord.model_validate(payload, strict=True)


class _CatalogStore:
    def list_run_repositories(self, repository_id: str | None = None) -> list[dict[str, str]]:
        rows = [
            {"run_id": "active", "repository_id": "repo", "repository": "/repo"},
            {"run_id": "done", "repository_id": "repo", "repository": "/repo"},
        ]
        return rows if repository_id in {None, "repo"} else []

    def list_records(self, _kind: str, _model: object) -> list[SimpleNamespace]:
        return []


def test_fleet_overview_separates_active_history_and_projects_persisted_attention() -> None:
    projections = {
        "active": {
            "state": "running",
            "generation": 2,
            "run": {"goal": {"statement": "Ship overview"}},
            "graph": {
                "nodes": [
                    {"id": "one", "name": "First task"},
                    {"id": "two", "name": "Current task"},
                ]
            },
            "nodes": [
                {
                    "node_id": "one",
                    "status": "passed",
                    "last_persisted_activity_at": "2026-01-01T00:00:05.000000Z",
                },
                {
                    "node_id": "two",
                    "status": "running",
                    "last_persisted_activity_at": "2026-01-01T00:00:10.123456Z",
                },
            ],
            "controls": [
                {
                    "action": "resume",
                    "created_at": "2026-01-01T00:00:15.234567Z",
                }
            ],
            "attention": [],
            "attention_count": 0,
            "attention_available": True,
        },
        "done": {
            "state": "failed",
            "generation": 1,
            "goal": {"statement": "Old goal"},
            "failure_code": "EVALUATION_FAILED",
            "graph": {"nodes": [{"id": "only", "name": "Only task"}]},
            "nodes": [
                {
                    "node_id": "only",
                    "status": "failed",
                    "last_persisted_activity_at": "2025-12-31T23:59:59.654321Z",
                }
            ],
            "controls": [],
            "attention": [
                {"kind": "task", "task_id": "only", "condition": "failed"},
                {"kind": "run", "condition": "EVALUATION_FAILED"},
            ],
            "attention_count": 2,
            "attention_available": True,
        },
    }
    with patch(
        "ai_employee.inspector.inspect_any_run",
        side_effect=lambda _store, run_id, **_kwargs: projections[run_id],
    ):
        result = inspect_fleet_runs(_CatalogStore(), "repo")  # type: ignore[arg-type]

    assert [item["run_id"] for item in result["active"]] == ["active"]
    assert [item["run_id"] for item in result["history"]] == ["done"]
    assert result["active"][0] == {
        "run_id": "active",
        "repository_id": "repo",
        "repository": "/repo",
        "goal": "Ship overview",
        "status": "running",
        "generation": 2,
        "progress": {"completed": 1, "total": 2},
        "active_task": "Current task",
        "active_tasks": [{"id": "two", "label": "Current task", "status": "running"}],
        "phase": "Task: Current task",
        "last_updated_at": "2026-01-01T00:00:15.234567Z",
        "requires_attention": False,
        "attention": [],
        "attention_count": 0,
        "attention_available": True,
    }
    assert result["history"][0]["attention"] == [
        {"kind": "task", "task_id": "only", "condition": "failed"},
        {"kind": "run", "condition": "EVALUATION_FAILED"},
    ]
    assert result["history"][0]["requires_attention"] is True
    assert result["history"][0]["attention_count"] == 2
    assert result["history"][0]["attention_available"] is True
    assert result["history"][0]["last_updated_at"] == "2025-12-31T23:59:59.654321Z"


def test_fleet_history_projects_deep_cause_ahead_of_node_wrapper() -> None:
    class Store(_CatalogStore):
        def list_run_repositories(self, repository_id: str | None = None) -> list[dict[str, str]]:
            return [{"run_id": "done", "repository_id": "repo", "repository": "/repo"}]

    projection = {
        "kind": "graph_run",
        "state": "failed",
        "generation": 3,
        "run": {"failure_code": "NODE_EXECUTION_FAILED"},
        "run_ownership": {"is_active": False},
        "nodes": [
            {
                "node_id": "failed-node",
                "generation": 3,
                "attempt": 2,
                "status": "failed",
                "failure_code": "NODE_EXECUTION_FAILED",
            }
        ],
        "worker_boundary_diagnostics": [
            {
                "id": "output-limit",
                "code": "PROCESS_OUTPUT_LIMIT_EXCEEDED",
                "stage": "process",
                "node_id": "failed-node",
                "generation": 3,
                "attempt": 2,
                "stdout_bytes": 4097,
                "stdout_limit_bytes": 4096,
                "stderr_bytes": 0,
                "stderr_limit_bytes": 4096,
                "output_limit_stream": "stdout",
            }
        ],
        "attention": [{"kind": "task", "task_id": "failed-node", "condition": "failed"}],
        "attention_count": 1,
        "attention_available": True,
    }
    with patch(
        "ai_employee.inspector.inspect_any_run",
        return_value=projection,
    ):
        result = inspect_fleet_runs(Store())  # type: ignore[arg-type]

    cause = result["history"][0]["primary_root_cause"]
    assert cause["code"] == "PROCESS_OUTPUT_LIMIT_EXCEEDED"
    assert cause["stage"] == "process"
    assert cause["stdout_bytes"] == 4097
    assert cause["stdout_limit_bytes"] == 4096
    assert cause["wrapper_context"]["code"] == "NODE_EXECUTION_FAILED"


def test_fleet_overview_hides_child_work_runs_and_prioritizes_attention() -> None:
    class Store(_CatalogStore):
        def list_run_repositories(self, repository_id: str | None = None) -> list[dict[str, str]]:
            return [
                {"run_id": "normal", "repository_id": "repo", "repository": "/repo"},
                {"run_id": "attention", "repository_id": "repo", "repository": "/repo"},
                {"run_id": "child", "repository_id": "repo", "repository": "/repo"},
            ]

        def list_records(self, _kind: str, _model: object) -> list[SimpleNamespace]:
            return [SimpleNamespace(work_run_id="child")]

    projections = {
        "normal": {
            "state": "running",
            "goal": "Normal",
            "nodes": [],
            "attention": [],
            "attention_count": 0,
            "attention_available": True,
        },
        "attention": {
            "state": "waiting_approval",
            "goal": "Needs approval",
            "nodes": [],
            "approvals": [{"decision": "pending"}],
            "attention": [
                {"kind": "run", "condition": "waiting_approval"},
                {"kind": "approval", "condition": "approval_required"},
            ],
            "attention_count": 2,
            "attention_available": True,
        },
    }
    with patch(
        "ai_employee.inspector.inspect_any_run",
        side_effect=lambda _store, run_id, **_kwargs: projections[run_id],
    ):
        result = inspect_fleet_runs(Store())  # type: ignore[arg-type]

    assert [item["run_id"] for item in result["active"]] == ["attention", "normal"]
    assert result["active"][0]["attention"] == [
        {"kind": "run", "condition": "waiting_approval"},
        {"kind": "approval", "condition": "approval_required"},
    ]


def test_fleet_overview_does_not_invent_missing_update_timestamp() -> None:
    class Store(_CatalogStore):
        def list_run_repositories(self, repository_id: str | None = None) -> list[dict[str, str]]:
            return [{"run_id": "old", "repository_id": "repo", "repository": "/repo"}]

    projection = {
        "state": "succeeded",
        "goal": "Older run",
        "nodes": [
            {
                "node_id": "old-node",
                "status": "passed",
                "created_at": "2025-01-01T00:00:00.000000Z",
            }
        ],
    }
    with patch(
        "ai_employee.inspector.inspect_any_run",
        return_value=projection,
    ):
        result = inspect_fleet_runs(Store())  # type: ignore[arg-type]

    assert result["history"][0]["last_updated_at"] is None
    assert "created_at" not in result["history"][0]
