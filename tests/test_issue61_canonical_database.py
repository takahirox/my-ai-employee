from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from ai_employee import cli
from ai_employee.database import import_legacy_database
from ai_employee.storage import SQLiteStore


def _subparsers(parser: argparse.ArgumentParser) -> list[argparse.ArgumentParser]:
    children: list[argparse.ArgumentParser] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            children.extend(action.choices.values())
            for child in action.choices.values():
                children.extend(_subparsers(child))
    return children


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_only_eval_parser_exposes_database_selection() -> None:
    parser = cli.build_parser()
    top_level = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    eval_parser = top_level.choices["eval"]
    assert any("--db" in action.option_strings for action in eval_parser._actions)
    for name, command_parser in top_level.choices.items():
        if name == "eval":
            continue
        for nested in [command_parser, *_subparsers(command_parser)]:
            assert all("--db" not in action.option_strings for action in nested._actions)


@pytest.mark.parametrize("value", ["", "/tmp/retired.db"])
def test_fleet_db_presence_fails_before_database_or_command_side_effects(
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FLEET_DB", value)
    with pytest.raises(SystemExit):
        cli.main(["inspect", "missing-run"])
    assert not (home / ".fleet").exists()
    assert "FLEET_DB is not supported" in capsys.readouterr().err


def test_eval_remains_isolated_when_retired_environment_variable_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_DB", "/does/not/control/eval.db")
    args = cli.build_parser().parse_args(
        ["eval", "scenario.yaml", "--strategy", "fixed", "--db", "experiment.db"]
    )
    assert args.db == "experiment.db"


def test_import_preserves_relationships_permissions_wal_and_is_idempotent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.db"
    destination = tmp_path / "fleet.db"
    repository = tmp_path / "repository"
    repository.mkdir()
    with SQLiteStore(source) as store:
        store.claim_run_id("legacy-run", repository)
    with SQLiteStore(destination) as store:
        store.claim_run_id("current-run", tmp_path)
    before = _digest(source)

    summary = import_legacy_database(source, destination=destination)

    assert summary["verified"] is True
    assert summary["already_imported"] is False
    assert summary["imported_rows"] == 2
    backup = Path(str(summary["backup"]))
    assert backup.is_file()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert _digest(source) == before
    with SQLiteStore(destination) as store:
        context = store.repository_for_run("legacy-run")
        assert context is not None
        assert context["repository"] == str(repository.resolve())
        assert str(store._connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"

    repeated = import_legacy_database(source, destination=destination)
    assert repeated["already_imported"] is True
    assert repeated["backup"] is None
    assert repeated["imported_rows"] == 0
    assert _digest(source) == before


def test_import_collision_rolls_back_every_source_row_and_journal(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.db"
    destination = tmp_path / "fleet.db"
    source_repository = tmp_path / "source-repository"
    destination_repository = tmp_path / "destination-repository"
    source_repository.mkdir()
    destination_repository.mkdir()
    with SQLiteStore(source) as store:
        store.claim_run_id("collision", source_repository)
    with SQLiteStore(destination) as store:
        store.claim_run_id("collision", destination_repository)
        original_repositories = store.list_run_repositories()

    with pytest.raises(ValueError, match="collision"):
        import_legacy_database(source, destination=destination)

    with SQLiteStore(destination) as store:
        assert store.list_run_repositories() == original_repositories
        assert store._connection.execute("SELECT COUNT(*) FROM legacy_imports").fetchone()[0] == 0
        source_id = store._connection.execute(
            "SELECT repository_id FROM repositories WHERE repository=?",
            (str(source_repository.resolve()),),
        ).fetchone()
        assert source_id is None


def test_unknown_source_schema_fails_without_creating_destination(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    destination = tmp_path / "fleet.db"
    with SQLiteStore(source):
        pass
    connection = sqlite3.connect(source)
    try:
        connection.execute("CREATE TABLE surprising_extension(id TEXT PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="unknown tables"):
        import_legacy_database(source, destination=destination)

    assert not destination.exists()
    assert not list(tmp_path.glob("fleet.db.backup-*"))


def test_import_rejects_active_source_without_touching_destination(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    destination = tmp_path / "fleet.db"
    with SQLiteStore(source):
        pass
    wal_marker = Path(f"{source}-wal")
    wal_marker.touch()

    with pytest.raises(ValueError, match="appears active"):
        import_legacy_database(source, destination=destination)

    assert not destination.exists()


def test_import_command_uses_canonical_home_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.db"
    home = tmp_path / "home"
    home.mkdir()
    with SQLiteStore(source):
        pass
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FLEET_DB", raising=False)

    assert cli.main(["import-legacy-db", str(source)]) == 0

    destination = home / ".fleet" / "fleet.db"
    assert destination.is_file()
    assert stat.S_IMODE(os.stat(destination).st_mode) == 0o600
