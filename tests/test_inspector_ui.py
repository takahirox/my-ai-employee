from html.parser import HTMLParser

from ai_employee.inspector import _INDEX


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
        "app",
        "details",
        "explanation",
        "graph",
        "raw",
        "revision",
        "revision-story",
        "run",
        "summary",
    }
    assert document.tabs == {"dag", "raw", "explanation"}
    assert document.buttons == {"inspect", "refresh"}

    for marker in (
        "Graph revision",
        "Raw Inspector record",
        "Run explanation record",
        "taskView",
        "renderGraph",
        "Typed result acceptances",
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
    assert "Promise.all([getJSON('/api/runs/'" in _INDEX
    assert "succeeded:'completed'" in _INDEX
    assert ".filter(x=>x.target_id===id).map(x=>x.source_id)" in _INDEX
    assert "Current unaccepted proposal" in _INDEX
    assert "story.graph?.accepted===true" in _INDEX
    assert "removed_task_summaries" in _INDEX
    assert "stateReasons" in _INDEX
