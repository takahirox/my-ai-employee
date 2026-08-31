from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

from ai_employee.inspector import _INDEX, inspect_fleet_runs


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
                {"node_id": "one", "status": "passed"},
                {"node_id": "two", "status": "running"},
            ],
            "controls": [],
        },
        "done": {
            "state": "failed",
            "generation": 1,
            "goal": {"statement": "Old goal"},
            "failure_code": "EVALUATION_FAILED",
            "graph": {"nodes": [{"id": "only", "name": "Only task"}]},
            "nodes": [{"node_id": "only", "status": "failed"}],
            "controls": [],
        },
    }
    with patch(
        "ai_employee.inspector.inspect_any_run",
        side_effect=lambda _store, run_id: projections[run_id],
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
        "requires_attention": False,
        "attention": [],
    }
    assert result["history"][0]["attention"] == [
        {"kind": "task", "task_id": "only", "condition": "failed"},
        {"kind": "run", "condition": "EVALUATION_FAILED"},
    ]
    assert result["history"][0]["requires_attention"] is True


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
        "normal": {"state": "running", "goal": "Normal", "nodes": []},
        "attention": {
            "state": "waiting_approval",
            "goal": "Needs approval",
            "nodes": [],
            "approvals": [{"decision": "pending"}],
        },
    }
    with patch(
        "ai_employee.inspector.inspect_any_run",
        side_effect=lambda _store, run_id: projections[run_id],
    ):
        result = inspect_fleet_runs(Store())  # type: ignore[arg-type]

    assert [item["run_id"] for item in result["active"]] == ["attention", "normal"]
    assert result["active"][0]["attention"] == [
        {"kind": "run", "condition": "waiting_approval"},
        {"kind": "approval", "condition": "approval_required"},
    ]
