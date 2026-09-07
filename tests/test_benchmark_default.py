import json
from types import SimpleNamespace

import pytest

from ai_employee import benchmark_default as connection


@pytest.mark.parametrize("completeness", [[], [True], [True, False]])
def test_default_connection_deducts_setup_and_keeps_live_failure_output(
    tmp_path, monkeypatch, capsys, completeness
):
    root, home, logs = (tmp_path / name for name in ("repo", "home", "logs"))
    root.mkdir()
    (home / "pocket").mkdir(parents=True)
    tick = iter((0.0, 10.0))
    monkeypatch.setattr(connection.time, "monotonic", lambda: next(tick))
    monkeypatch.setattr(
        connection.shutil, "copyfile", lambda _src, dst: dst.write_text("# fixture")
    )
    monkeypatch.setattr(connection, "execute", lambda *args, **kwargs: b"")

    monkeypatch.setattr(connection, "inspect_profile", lambda *_args: {})
    monkeypatch.setattr(
        connection,
        "inspect_usage",
        lambda *_args: {"invocation_details": [{"complete": value} for value in completeness]},
    )

    def product(argv, **kwargs):
        harness = json.loads((root / ".fleet/project.json").read_text())
        assert harness["budgets"]["wall_seconds"] == 168.0
        assert harness["worker"]["adaptive_routing"] is True
        assert "capture_output" not in kwargs
        kwargs["stdout"].write('{"run_id":"fixture-run","status":"failed","stable_code":"TIMEOUT"}')
        kwargs["stdout"].flush()
        assert json.loads((logs / "fleet-result.json").read_text())["status"] == "failed"
        kwargs["stderr"].write("diagnostic fixture")
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(connection.subprocess, "run", product)
    assert connection.run({"instruction": "fixture", "seconds": 180.0}, root, home, logs) == 0
    assert (logs / "fleet.stderr").read_text() == "diagnostic fixture"
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    usage = next(event for event in events if event["type"] == "pocket.usage")
    assert usage["usage"] == {}
    assert usage["complete"] is (bool(completeness) and all(completeness))
    assert (logs / "fleet-diagnostics.json").exists()


@pytest.mark.parametrize("seconds", [0, -1, True, float("nan"), float("inf")])
def test_default_connection_does_not_start_with_invalid_allowance(tmp_path, seconds):
    with pytest.raises(ValueError):
        connection.run({"seconds": seconds}, tmp_path, tmp_path, tmp_path)
