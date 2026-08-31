from __future__ import annotations

import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_employee import cli
from ai_employee.database import DEFAULT_DATABASE_PATH, resolve_database_path
from ai_employee.domain import Goal
from ai_employee.storage import SQLiteStore


def test_database_resolution_precedence_and_home_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    environment = {"FLEET_DB": "~/environment.db"}

    assert resolve_database_path("~/explicit.db", environment=environment) == home / "explicit.db"
    assert resolve_database_path(None, environment=environment) == home / "environment.db"
    assert resolve_database_path(None, environment={}) == Path(DEFAULT_DATABASE_PATH).expanduser()
    with pytest.raises(ValueError, match="must not be empty"):
        resolve_database_path(None, environment={"FLEET_DB": ""})


def test_normal_cli_uses_global_default_environment_and_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repository)

    assert cli.main(["demo", "--run-id", "default-run"]) == 0
    capsys.readouterr()
    default_database = home / ".fleet" / "fleet.db"
    assert default_database.is_file()
    assert not (repository / ".fleet" / "fleet.db").exists()

    environment_database = tmp_path / "environment.db"
    monkeypatch.setenv("FLEET_DB", str(environment_database))
    assert cli.main(["demo", "--run-id", "environment-run"]) == 0
    capsys.readouterr()
    assert environment_database.is_file()

    explicit_database = tmp_path / "explicit.db"
    assert cli.main(["demo", "--run-id", "explicit-run", "--db", str(explicit_database)]) == 0
    capsys.readouterr()
    assert explicit_database.is_file()


def test_eval_database_remains_intentionally_repository_local() -> None:
    args = cli.build_parser().parse_args(["eval", "scenario.yaml", "--strategy", "strategy"])
    assert args.db == ".fleet/evals.db"


def test_database_and_new_parent_permissions_are_private(tmp_path: Path) -> None:
    database = tmp_path / "state" / "nested" / "fleet.db"
    with SQLiteStore(database):
        pass

    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.parent.parent.stat().st_mode) == 0o700

    database.chmod(0o666)
    with SQLiteStore(database):
        pass
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_sqlite_wal_busy_timeout_and_concurrent_writers(tmp_path: Path) -> None:
    database = tmp_path / "fleet.db"
    with SQLiteStore(database) as store:
        assert store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000

    def write(prefix: str) -> None:
        with SQLiteStore(database) as writer:
            for index in range(20):
                goal = Goal(id=f"{prefix}-{index}", statement=f"goal {prefix} {index}")
                writer.put("concurrent_goal", goal, run_id=goal.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(write, "left"), executor.submit(write, "right"))
        for future in futures:
            future.result(timeout=15)

    with SQLiteStore(database) as store:
        assert len(store.list_records("concurrent_goal", Goal)) == 40


def test_repository_filtering_legacy_visibility_and_run_id_collisions(tmp_path: Path) -> None:
    first_repository = tmp_path / "first"
    second_repository = tmp_path / "second"
    first_repository.mkdir()
    second_repository.mkdir()

    with SQLiteStore(tmp_path / "fleet.db") as store:
        store.claim_run_id("first-run", first_repository)
        store.claim_run_id("second-run", second_repository)
        legacy = Goal(id="legacy-goal", statement="legacy")
        store.put("goal", legacy, run_id="legacy-run")

        first_context = store.repository_for_run("first-run")
        second_context = store.repository_for_run("second-run")
        assert first_context is not None
        assert second_context is not None
        assert first_context["repository_id"] != second_context["repository_id"]

        first_runs = store.list_run_repositories(first_context["repository_id"])
        assert [item["run_id"] for item in first_runs] == ["first-run"]
        all_runs = store.list_run_repositories()
        assert {item["run_id"] for item in all_runs} == {
            "first-run",
            "second-run",
            "legacy-run",
        }
        assert next(item for item in all_runs if item["run_id"] == "legacy-run") == {
            "run_id": "legacy-run",
            "repository_id": None,
            "repository": None,
        }

        with pytest.raises(ValueError, match=r"already exists.*different --run-id"):
            store.claim_run_id("first-run", first_repository)
        with pytest.raises(ValueError, match=r"already exists.*different --run-id"):
            store.claim_run_id("legacy-run", second_repository)
