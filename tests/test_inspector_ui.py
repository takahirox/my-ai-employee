from ai_employee.inspector import _INDEX


def test_inspector_ui_exposes_read_only_dag_and_task_detail_contract() -> None:
    for marker in (
        "Accepted revision",
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
