from __future__ import annotations

import argparse
import json
import shlex
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


def test_run_explicit_temporary_database_warns_on_stderr_and_keeps_json_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    temporary_directory = tmp_path / "platform temporary directory"
    temporary_directory.mkdir()
    database = temporary_directory / "run.db"
    monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(temporary_directory))

    assert cli.main(["run", _graph(), "--run-id", "temporary-run", "--db", str(database)]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"run_id": "temporary-run", "state": "succeeded"}
    resolved = database.resolve()
    assert captured.err == (
        f"warning: temporary database {resolved} is absent from the default Inspector; "
        f"run `{shlex.join(('fleet', 'serve', '--db', str(resolved)))}` to inspect it.\n"
    )


def test_work_environment_temporary_database_warns_and_keeps_json_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    temporary_directory = tmp_path / "platform-temporary-directory"
    temporary_directory.mkdir()
    database = temporary_directory / "work.db"
    monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(temporary_directory))
    monkeypatch.setenv("FLEET_DB", str(database))

    def fake_work(args: argparse.Namespace) -> int:
        assert args.db == str(database)
        print('{"run_id":"temporary-work","status":"planned"}')
        return 0

    monkeypatch.setattr(cli, "_work", fake_work)
    assert cli.main(["work", "plan a bounded change", "--json"]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"run_id": "temporary-work", "status": "planned"}
    resolved = database.resolve()
    assert captured.err == (
        f"warning: temporary database {resolved} is absent from the default Inspector; "
        f"run `fleet serve --db {resolved}` to inspect it.\n"
    )


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
    calls: list[tuple[Path, str, int]] = []

    def fake_serve(store: SQLiteStore, host: str, port: int) -> None:
        calls.append((Path(store.path).resolve(), host, port))

    monkeypatch.setattr(cli, "serve", fake_serve)
    assert cli.main(["serve", "--db", "inspector.db"]) == 0

    database = (tmp_path / "inspector.db").resolve()
    captured = capsys.readouterr()
    assert captured.out == (f"Fleet Inspector: http://127.0.0.1:8765 (database: {database})\n")
    assert captured.err == ""
    assert calls == [(database, "127.0.0.1", 8765)]
