from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from ai_employee import cli
from ai_employee.database import DEFAULT_DATABASE_PATH, resolve_database_path
from ai_employee.storage import SQLiteStore


def _graph() -> str:
    return str(Path(__file__).parents[1] / "examples" / "demo_graph.yaml")


def test_database_resolution_and_eval_semantics_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    environment = {"FLEET_DB": "~/environment.db"}

    assert resolve_database_path("~/explicit.db", environment=environment) == (home / "explicit.db")
    assert resolve_database_path(None, environment=environment) == home / "environment.db"
    assert resolve_database_path(None, environment={}) == (Path(DEFAULT_DATABASE_PATH).expanduser())

    monkeypatch.setenv("FLEET_DB", "/ignored/environment.db")
    default_eval = cli.build_parser().parse_args(
        ["eval", "scenario.yaml", "--strategy", "strategy"]
    )
    explicit_eval = cli.build_parser().parse_args(
        ["eval", "scenario.yaml", "--strategy", "strategy", "--db", "custom-evals.db"]
    )
    assert default_eval.db == ".fleet/evals.db"
    assert explicit_eval.db == "custom-evals.db"


def test_run_database_override_is_not_a_parser_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "run.db"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FLEET_DB", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["run", _graph(), "--run-id", "temporary-run", "--db", str(database)])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unrecognized arguments: --db" in captured.err
    assert not database.exists()
    assert not (home / ".fleet").exists()


def test_work_rejects_database_environment_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    temporary_directory = tmp_path / "platform-temporary-directory"
    temporary_directory.mkdir()
    database = temporary_directory / "work.db"
    monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(temporary_directory))
    monkeypatch.setenv("FLEET_DB", str(database))
    called = False

    def fake_work(args: argparse.Namespace) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(cli, "_work", fake_work)
    with pytest.raises(SystemExit):
        cli.main(["work", "plan a bounded change", "--json"])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "FLEET_DB is not supported" in captured.err
    assert called is False
    assert not database.exists()


def test_default_and_unrelated_commands_do_not_warn_for_temporary_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    temporary_directory = tmp_path / "platform-temporary-directory"
    temporary_directory.mkdir()
    home = temporary_directory / "home"
    home.mkdir()
    monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(temporary_directory))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FLEET_DB", raising=False)

    assert cli.main(["run", _graph(), "--run-id", "shared-default-run"]) == 0
    assert capsys.readouterr().err == ""

    cli._warn_for_explicit_temporary_database("run", str(tmp_path / "tmp-sibling" / "db.sqlite"))
    cli._warn_for_explicit_temporary_database("inspect", str(temporary_directory / "other.db"))
    assert capsys.readouterr().err == ""


def test_serve_reports_loopback_url_and_resolved_database_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FLEET_DB", raising=False)
    calls: list[tuple[Path, str, int]] = []

    def fake_serve(store: SQLiteStore, host: str, port: int) -> None:
        calls.append((Path(store.path).resolve(), host, port))

    monkeypatch.setattr(cli, "serve", fake_serve)
    assert cli.main(["serve"]) == 0

    database = (home / ".fleet" / "fleet.db").resolve()
    captured = capsys.readouterr()
    assert captured.out == (f"Fleet Inspector: http://127.0.0.1:8765 (database: {database})\n")
    assert captured.err == ""
    assert calls == [(database, "127.0.0.1", 8765)]
