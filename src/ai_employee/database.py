"""Canonical database selection and safe legacy database import."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

DEFAULT_DATABASE_PATH = "~/.fleet/fleet.db"
DATABASE_ENVIRONMENT_VARIABLE = "FLEET_DB"

_BASE_TABLES: Final = (
    "repositories",
    "records",
    "events",
    "checkpoints",
    "strategy_performance",
    "controls",
    "run_repositories",
)
_V2_TABLES: Final = (
    "work_events_v2",
    "work_checkpoints_v2",
    "graph_claims_v2",
    "graph_reservations_v2",
    "run_execution_owners_v2",
)
_TABLE_SPECS: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    "fleet_meta": (("key", "value"), ("key",)),
    "records": (
        ("kind", "record_id", "run_id", "revision", "payload"),
        ("kind", "record_id", "revision"),
    ),
    "events": (
        ("sequence", "event_id", "run_id", "event_type", "payload"),
        ("sequence",),
    ),
    "checkpoints": (("run_id", "generation", "payload"), ("run_id",)),
    "strategy_performance": (
        ("profile_id", "strategy_id", "payload"),
        ("profile_id", "strategy_id"),
    ),
    "controls": (("run_id", "action"), ("run_id",)),
    "repositories": (("repository_id", "repository"), ("repository_id",)),
    "run_repositories": (("run_id", "repository_id"), ("run_id",)),
    "work_events_v2": (("sequence", "event_id", "run_id", "payload"), ("sequence",)),
    "work_checkpoints_v2": (("run_id", "generation", "payload"), ("run_id",)),
    "graph_claims_v2": (("run_id", "node_id"), ("run_id", "node_id")),
    "graph_reservations_v2": (
        (
            "run_id",
            "node_id",
            "generation",
            "attempt",
            "worker_turns",
            "processes",
            "wall_seconds",
            "artifact_bytes",
        ),
        ("run_id", "node_id", "generation", "attempt"),
    ),
    "run_execution_owners_v2": (
        (
            "run_id",
            "owner_record_id",
            "owner_record_digest",
            "graph_revision_digest",
            "generation",
            "execution_attempt",
            "owner_instance_id",
            "last_heartbeat_at",
            "expires_at",
            "heartbeat_digest",
            "status",
            "closure_record_id",
            "closure_record_digest",
        ),
        ("run_id",),
    ),
    "legacy_imports": (
        (
            "source_digest",
            "source_path",
            "imported_at",
            "source_schema_version",
            "source_rows",
            "imported_rows",
            "skipped_rows",
        ),
        ("source_digest",),
    ),
}


def resolve_database_path(
    explicit: str | Path | None,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve a database path for dependency-injected and compatibility callers.

    Operational CLI commands deliberately call this without either override. Keeping
    this API avoids breaking tests and embedders which construct an isolated SQLiteStore.
    """

    values = os.environ if environment is None else environment
    selected: str | Path | None = explicit
    if selected is None:
        selected = values.get(DATABASE_ENVIRONMENT_VARIABLE, DEFAULT_DATABASE_PATH)
    if not str(selected).strip():
        raise ValueError("Fleet database path must not be empty")
    return Path(selected).expanduser()


def reject_database_environment(environment: Mapping[str, str] | None = None) -> None:
    """Reject the retired operational database environment override by presence."""

    values = os.environ if environment is None else environment
    if DATABASE_ENVIRONMENT_VARIABLE in values:
        raise ValueError(
            "FLEET_DB is not supported for operational commands; "
            f"Fleet always uses {DEFAULT_DATABASE_PATH}"
        )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_signature(path: Path) -> tuple[int, int, int, int, int, int]:
    details = path.stat()
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _read_only_connection(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    options = "mode=ro&immutable=1" if immutable else "mode=ro"
    connection = sqlite3.connect(f"{path.as_uri()}?{options}", uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _validate_source(
    connection: sqlite3.Connection,
) -> tuple[int, tuple[str, ...], dict[str, int]]:
    check = connection.execute("PRAGMA quick_check").fetchone()
    if check is None or str(check[0]).lower() != "ok":
        raise ValueError("legacy database failed SQLite integrity validation")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise ValueError("legacy database contains broken foreign-key relationships")

    objects = connection.execute(
        "SELECT type,name FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    tables = {str(row[1]) for row in objects if row[0] == "table"}
    unsupported_objects = [str(row[1]) for row in objects if row[0] not in {"index", "table"}]
    if unsupported_objects:
        raise ValueError(
            "legacy database contains unsupported schema objects: "
            + ", ".join(unsupported_objects)
        )
    unknown = tables - _TABLE_SPECS.keys()
    if unknown:
        raise ValueError("legacy database contains unknown tables: " + ", ".join(sorted(unknown)))
    required = {
        "fleet_meta",
        "records",
        "events",
        "checkpoints",
        "strategy_performance",
        "controls",
    }
    missing = required - tables
    if missing:
        raise ValueError(
            "legacy database is missing required tables: " + ", ".join(sorted(missing))
        )

    for table in sorted(tables):
        expected_columns = _TABLE_SPECS[table][0]
        actual_columns = tuple(
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        if actual_columns != expected_columns:
            raise ValueError(f"legacy database has an unknown {table} schema")

    version_row = connection.execute(
        "SELECT value FROM fleet_meta WHERE key='schema_version'"
    ).fetchone()
    if version_row is None:
        raise ValueError("legacy database has no Fleet schema version")
    try:
        version = int(version_row[0])
    except (TypeError, ValueError) as error:
        raise ValueError("legacy database has an invalid Fleet schema version") from error
    if version not in {1, 2}:
        raise ValueError(f"legacy database schema version {version} is unsupported")
    if version == 1 and tables.intersection(_V2_TABLES):
        raise ValueError("legacy database schema version does not match its v2 tables")

    ordered_tables = tuple(
        table for table in (*_BASE_TABLES, *_V2_TABLES) if table in tables
    )
    counts = {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in ordered_tables
    }
    return version, ordered_tables, counts


def _already_imported(destination: Path, source_digest: str) -> bool:
    if not destination.is_file():
        return False
    connection = _read_only_connection(destination)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_imports'"
        ).fetchone()
        if table is None:
            return False
        return (
            connection.execute(
                "SELECT 1 FROM legacy_imports WHERE source_digest=?", (source_digest,)
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


def _backup_destination(destination: Path, source_digest: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    counter = 0
    while True:
        suffix = "" if counter == 0 else f"-{counter}"
        candidate = destination.with_name(
            f"{destination.name}.backup-{timestamp}-{source_digest[:12]}{suffix}"
        )
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            counter += 1
            continue
        os.close(descriptor)
        break
    try:
        source = _read_only_connection(destination)
        backup = sqlite3.connect(candidate)
        try:
            source.backup(backup)
        finally:
            backup.close()
            source.close()
        os.chmod(candidate, 0o600)
        return candidate
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise


def _copy_table(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    table: str,
) -> tuple[int, int]:
    columns, primary_key = _TABLE_SPECS[table]
    quoted_columns = ",".join(f'"{column}"' for column in columns)
    placeholders = ",".join("?" for _column in columns)
    imported = 0
    skipped = 0
    for row in source.execute(f'SELECT {quoted_columns} FROM "{table}"'):
        values = tuple(row[column] for column in columns)
        try:
            destination.execute(
                f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})', values
            )
        except sqlite3.IntegrityError as error:
            predicate = " AND ".join(f'"{column}"=?' for column in primary_key)
            key_values = tuple(row[column] for column in primary_key)
            existing = destination.execute(
                f'SELECT {quoted_columns} FROM "{table}" WHERE {predicate}', key_values
            ).fetchone()
            if existing is None or tuple(existing[column] for column in columns) != values:
                raise ValueError(
                    f"legacy import collision in {table} for key {key_values!r}"
                ) from error
            skipped += 1
        else:
            imported += 1
    return imported, skipped


def import_legacy_database(
    source: str | Path,
    *,
    destination: str | Path | None = None,
) -> dict[str, object]:
    """Safely merge one stopped legacy Fleet database into the canonical store."""

    raw_source = Path(source).expanduser()
    if raw_source.is_symlink() or not raw_source.is_file():
        raise ValueError("legacy database source must be an existing regular non-symlink file")
    source_path = raw_source.resolve()
    destination_path = resolve_database_path(
        destination, environment={} if destination is None else None
    ).resolve()
    if destination_path.is_symlink():
        raise ValueError("canonical database destination must not be a symlink")
    if source_path == destination_path:
        raise ValueError("legacy database source and canonical destination must differ")
    for suffix in ("-wal", "-shm"):
        if Path(f"{source_path}{suffix}").exists():
            raise ValueError("legacy database appears active; stop Fleet and checkpoint it first")

    initial_signature = _file_signature(source_path)
    source_digest = _file_digest(source_path)
    source_connection = _read_only_connection(source_path, immutable=True)
    source_connection.execute("BEGIN")
    try:
        schema_version, tables, source_counts = _validate_source(source_connection)
        source_rows = sum(source_counts.values())
        if _already_imported(destination_path, source_digest):
            return {
                "schema_version": "2",
                "source": str(source_path),
                "source_digest": source_digest,
                "destination": str(destination_path),
                "backup": None,
                "source_rows": source_rows,
                "imported_rows": 0,
                "skipped_rows": source_rows,
                "already_imported": True,
                "verified": True,
            }

        existed = destination_path.is_file()
        backup_path = (
            _backup_destination(destination_path, source_digest) if existed else None
        )

        from .storage import SQLiteStore

        with SQLiteStore(destination_path) as store:
            if schema_version == 2:
                store.migrate_v2()
            connection = store._connection
            imported_rows = 0
            skipped_rows = 0
            try:
                connection.execute("BEGIN IMMEDIATE")
                for table in tables:
                    imported, skipped = _copy_table(source_connection, connection, table)
                    imported_rows += imported
                    skipped_rows += skipped
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("legacy import would break destination relationships")
                if any(Path(f"{source_path}{suffix}").exists() for suffix in ("-wal", "-shm")):
                    raise ValueError(
                        "legacy database appears active; stop Fleet and checkpoint it first"
                    )
                if (
                    _file_signature(source_path) != initial_signature
                    or _file_digest(source_path) != source_digest
                ):
                    raise ValueError("legacy database changed during import")
                imported_at = datetime.now(UTC).isoformat()
                connection.execute(
                    "INSERT INTO legacy_imports("
                    "source_digest,source_path,imported_at,source_schema_version,"
                    "source_rows,imported_rows,skipped_rows) VALUES(?,?,?,?,?,?,?)",
                    (
                        source_digest,
                        str(source_path),
                        imported_at,
                        schema_version,
                        source_rows,
                        imported_rows,
                        skipped_rows,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            os.chmod(destination_path, 0o600)
            journal = store._connection.execute(
                "SELECT source_rows,imported_rows,skipped_rows FROM legacy_imports "
                "WHERE source_digest=?",
                (source_digest,),
            ).fetchone()
            if journal is None or tuple(journal) != (
                source_rows,
                imported_rows,
                skipped_rows,
            ):
                raise RuntimeError("legacy import verification journal is missing or stale")
            journal_mode = str(store._connection.execute("PRAGMA journal_mode").fetchone()[0])
            if journal_mode.lower() != "wal":
                raise RuntimeError("canonical database lost required WAL journaling")
        return {
            "schema_version": "2",
            "source": str(source_path),
            "source_digest": source_digest,
            "destination": str(destination_path),
            "backup": None if backup_path is None else str(backup_path),
            "source_rows": source_rows,
            "imported_rows": imported_rows,
            "skipped_rows": skipped_rows,
            "already_imported": False,
            "verified": True,
        }
    finally:
        source_connection.rollback()
        source_connection.close()
