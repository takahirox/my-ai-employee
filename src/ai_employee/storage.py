"""SQLite persistence boundary for authoritative runtime facts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .domain import (
    AcceptedGraphRevision,
    Artifact,
    Event,
    ExecutionMetrics,
    Node,
    ProjectProfile,
    Run,
    StateTransition,
    StrategyPerformance,
    Task,
    VerificationEvidence,
)
from .serialization import canonical_json

ModelT = TypeVar("ModelT", bound=BaseModel)


def _repository_details(repository: str | Path) -> tuple[str, str]:
    """Return a stable local identity and display path for a repository."""

    root = Path(repository).expanduser().resolve()
    anchor = root
    dot_git = root / ".git"
    if dot_git.is_dir():
        anchor = dot_git.resolve()
    elif dot_git.is_file():
        lines = dot_git.read_text(encoding="utf-8").splitlines()
        if lines and lines[0].startswith("gitdir: "):
            git_directory = Path(lines[0].removeprefix("gitdir: "))
            if not git_directory.is_absolute():
                git_directory = (root / git_directory).resolve()
            common_directory = git_directory / "commondir"
            anchor = (
                (git_directory / common_directory.read_text(encoding="utf-8").strip()).resolve()
                if common_directory.is_file()
                else git_directory.resolve()
            )
    digest = hashlib.sha256(f"fleet-repository-v1\0{anchor}".encode()).hexdigest()
    return digest, str(root)


def _prepare_database_file(path: Path) -> None:
    """Create a private, non-symlink SQLite target and any missing parents."""

    missing: list[Path] = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)

    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


class SQLiteStore:
    """Small vendor-neutral storage API backed by SQLite by default."""

    def __init__(self, path: str | Path = "~/.fleet/fleet.db") -> None:
        self.path = str(Path(path).expanduser()) if str(path) != ":memory:" else ":memory:"
        if self.path != ":memory:":
            _prepare_database_file(Path(self.path))
        self._connection = sqlite3.connect(self.path, timeout=10.0)
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 10000")
            if self.path != ":memory:":
                journal = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()
                if journal is None or str(journal[0]).lower() != "wal":
                    raise RuntimeError("Fleet database does not support required WAL journaling")
            self._create_schema()
            if self.path != ":memory:":
                os.chmod(self.path, 0o600)
        except BaseException:
            self._connection.close()
            raise

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection:
            yield self._connection

    def _create_schema(self) -> None:
        existing = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='fleet_meta'"
        ).fetchone()
        if existing is not None:
            row = self._connection.execute(
                "SELECT value FROM fleet_meta WHERE key='schema_version'"
            ).fetchone()
            if row is not None and int(row[0]) > 2:
                raise ValueError("database schema is newer than this Fleet version")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS fleet_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL,
                record_id TEXT NOT NULL,
                run_id TEXT,
                revision INTEGER NOT NULL DEFAULT 1,
                payload TEXT NOT NULL,
                PRIMARY KEY (kind, record_id, revision)
            );
            CREATE INDEX IF NOT EXISTS records_run ON records(run_id, kind);
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id, sequence);
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategy_performance (
                profile_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(profile_id, strategy_id)
            );
            CREATE TABLE IF NOT EXISTS controls (
                run_id TEXT PRIMARY KEY,
                action TEXT NOT NULL CHECK(action IN ('pause', 'cancel'))
            );
            CREATE TABLE IF NOT EXISTS repositories (
                repository_id TEXT PRIMARY KEY,
                repository TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS run_repositories (
                run_id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL,
                FOREIGN KEY(repository_id) REFERENCES repositories(repository_id)
            );
            CREATE INDEX IF NOT EXISTS run_repositories_repository
                ON run_repositories(repository_id, run_id);
            """
        )
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO fleet_meta(key, value) VALUES('schema_version', '1')"
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO fleet_meta(key, value) VALUES('fleet_version', '0.2.1')"
            )

    def _schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT value FROM fleet_meta WHERE key='schema_version'"
        ).fetchone()
        return 1 if row is None else int(row[0])

    def migrate_v2(self) -> None:
        """Transactionally add v2 projections only when a v2 write is requested."""
        with self.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS work_events_v2 ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT,"
                "event_id TEXT NOT NULL UNIQUE,run_id TEXT NOT NULL,payload TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS work_events_v2_run ON work_events_v2(run_id, sequence)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS work_checkpoints_v2 ("
                "run_id TEXT PRIMARY KEY,generation INTEGER NOT NULL,payload TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS graph_claims_v2 ("
                "run_id TEXT NOT NULL,node_id TEXT NOT NULL,"
                "PRIMARY KEY(run_id,node_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS graph_reservations_v2 ("
                "run_id TEXT NOT NULL,node_id TEXT NOT NULL,generation INTEGER NOT NULL,"
                "attempt INTEGER NOT NULL,worker_turns INTEGER NOT NULL,"
                "processes INTEGER NOT NULL,wall_seconds REAL NOT NULL,"
                "artifact_bytes INTEGER NOT NULL,"
                "PRIMARY KEY(run_id,node_id,generation,attempt))"
            )
            connection.execute(
                "INSERT OR REPLACE INTO fleet_meta(key,value) VALUES('schema_version','2')"
            )
            connection.execute(
                "INSERT OR REPLACE INTO fleet_meta(key,value) VALUES('fleet_version','0.2.1')"
            )

    def put(
        self,
        kind: str,
        model: BaseModel,
        *,
        run_id: str | None = None,
        revision: int = 1,
    ) -> None:
        record_id = getattr(model, "id", None)
        if record_id is None and isinstance(model, AcceptedGraphRevision):
            record_id = model.graph.id
        if record_id is None:
            raise ValueError("stored model requires an id")
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO records"
                "(kind, record_id, run_id, revision, payload) VALUES(?,?,?,?,?)",
                (kind, record_id, run_id, revision, canonical_json(model)),
            )

    def put_once(
        self,
        kind: str,
        model: BaseModel,
        *,
        run_id: str | None = None,
        revision: int = 1,
    ) -> bool:
        """Persist a record revision only if that exact key is still unused."""
        record_id = getattr(model, "id", None)
        if record_id is None:
            raise ValueError("stored model requires an id")
        with self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO records"
                "(kind, record_id, run_id, revision, payload) VALUES(?,?,?,?,?)",
                (kind, record_id, run_id, revision, canonical_json(model)),
            )
        return cursor.rowcount == 1

    def get(
        self, kind: str, record_id: str, model_type: type[ModelT], *, revision: int | None = None
    ) -> ModelT:
        if revision is None:
            row = self._connection.execute(
                "SELECT payload FROM records WHERE kind=? AND record_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (kind, record_id),
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT payload FROM records WHERE kind=? AND record_id=? AND revision=?",
                (kind, record_id, revision),
            ).fetchone()
        if row is None:
            raise KeyError((kind, record_id, revision))
        return model_type.model_validate_json(row["payload"], strict=True)

    def list_records(
        self, kind: str, model_type: type[ModelT], *, run_id: str | None = None
    ) -> tuple[ModelT, ...]:
        if run_id is None:
            rows = self._connection.execute(
                "SELECT payload FROM records WHERE kind=? ORDER BY record_id, revision", (kind,)
            )
        else:
            rows = self._connection.execute(
                "SELECT payload FROM records WHERE kind=? AND run_id=? "
                "ORDER BY record_id, revision",
                (kind, run_id),
            )
        return tuple(model_type.model_validate_json(row["payload"], strict=True) for row in rows)

    def save_run(self, run: Run) -> None:
        self.put("run", run, run_id=run.id, revision=run.generation + 1)

    def claim_run_id(self, run_id: str, repository: str | Path) -> None:
        """Atomically bind a new run ID to one repository without overwriting history."""

        repository_id, normalized = _repository_details(repository)
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            claimed = connection.execute(
                "SELECT 1 FROM run_repositories WHERE run_id=?", (run_id,)
            ).fetchone()
            persisted = connection.execute(
                "SELECT 1 FROM records WHERE run_id=? LIMIT 1", (run_id,)
            ).fetchone()
            if claimed is not None or persisted is not None:
                raise ValueError(f"run ID {run_id!r} already exists; choose a different --run-id")
            connection.execute(
                "INSERT OR IGNORE INTO repositories(repository_id,repository) VALUES(?,?)",
                (repository_id, normalized),
            )
            connection.execute(
                "INSERT INTO run_repositories(run_id,repository_id) VALUES(?,?)",
                (run_id, repository_id),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def repository_for_run(self, run_id: str) -> dict[str, str] | None:
        row = self._connection.execute(
            "SELECT repositories.repository_id,repositories.repository "
            "FROM run_repositories JOIN repositories USING(repository_id) "
            "WHERE run_repositories.run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return {"repository_id": str(row[0]), "repository": str(row[1])}

    def list_run_repositories(
        self, repository_id: str | None = None
    ) -> tuple[dict[str, str | None], ...]:
        """List registered and legacy run IDs, optionally filtered by repository."""

        rows = self._connection.execute(
            "WITH run_ids(run_id) AS ("
            "SELECT run_id FROM run_repositories "
            "UNION SELECT DISTINCT run_id FROM records WHERE run_id IS NOT NULL"
            ") SELECT run_ids.run_id,run_repositories.repository_id,repositories.repository "
            "FROM run_ids "
            "LEFT JOIN run_repositories USING(run_id) "
            "LEFT JOIN repositories USING(repository_id) "
            "WHERE (? IS NULL OR run_repositories.repository_id=?) "
            "ORDER BY COALESCE(repositories.repository,''),run_ids.run_id",
            (repository_id, repository_id),
        )
        return tuple(
            {
                "run_id": str(row[0]),
                "repository_id": None if row[1] is None else str(row[1]),
                "repository": None if row[2] is None else str(row[2]),
            }
            for row in rows
        )

    def save_graph(self, run_id: str, revision: AcceptedGraphRevision) -> None:
        self.put("graph", revision, run_id=run_id, revision=revision.revision_number)

    def save_task(self, run_id: str, task: Task) -> None:
        self.put("task", task, run_id=run_id, revision=task.attempt + 1)

    def save_node(self, run_id: str, node: Node) -> None:
        self.put("node", node, run_id=run_id, revision=node.attempt + 1)

    def save_artifact(self, artifact: Artifact) -> None:
        self.put("artifact", artifact, run_id=artifact.run_id)

    def save_evidence(self, run_id: str, evidence: VerificationEvidence) -> None:
        self.put("evidence", evidence, run_id=run_id)

    def save_metrics(self, metrics: ExecutionMetrics) -> None:
        self.put("metrics", metrics, run_id=metrics.run_id)

    def save_profile(self, profile: ProjectProfile, *, revision: int = 1) -> None:
        self.put("profile", profile, revision=revision)

    def append_event(self, event: Event) -> int:
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO events(event_id, run_id, event_type, payload) VALUES(?,?,?,?)",
                (event.id, event.run_id, event.event_type, canonical_json(event)),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return an event sequence")
        return cursor.lastrowid

    def events(self, run_id: str) -> tuple[Event, ...]:
        rows = self._connection.execute(
            "SELECT payload FROM events WHERE run_id=? ORDER BY sequence", (run_id,)
        )
        return tuple(Event.model_validate_json(row["payload"], strict=True) for row in rows)

    def checkpoint(self, run_id: str, generation: int, payload: object) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO checkpoints(run_id, generation, payload) VALUES(?,?,?)",
                (run_id, generation, canonical_json(payload)),
            )

    def load_checkpoint(self, run_id: str) -> tuple[int, dict[str, Any]]:
        row = self._connection.execute(
            "SELECT generation, payload FROM checkpoints WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        payload = json.loads(row["payload"])
        if not isinstance(payload, dict):
            raise ValueError("checkpoint payload must be an object")
        return int(row["generation"]), payload

    def save_transition(self, run_id: str, transition: StateTransition, *, sequence: int) -> None:
        self.put(
            "transition",
            _TransitionRecord.from_transition(sequence, transition),
            run_id=run_id,
            revision=sequence,
        )

    def save_performance(self, profile_id: str, performance: StrategyPerformance) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO strategy_performance"
                "(profile_id, strategy_id, payload) VALUES(?,?,?)",
                (profile_id, performance.strategy_id, canonical_json(performance)),
            )

    def performance(self, profile_id: str) -> tuple[StrategyPerformance, ...]:
        rows = self._connection.execute(
            "SELECT payload FROM strategy_performance WHERE profile_id=? ORDER BY strategy_id",
            (profile_id,),
        )
        return tuple(
            StrategyPerformance.model_validate_json(row["payload"], strict=True) for row in rows
        )

    def request_control(self, run_id: str, action: str) -> None:
        if action not in {"pause", "cancel"}:
            raise ValueError("control action must be pause or cancel")
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR REPLACE INTO controls(run_id, action) VALUES(?,?)", (run_id, action)
            )
            if action == "cancel":
                row = connection.execute(
                    "SELECT revision,payload FROM records "
                    "WHERE kind='graph_run_v2' AND record_id=? "
                    "ORDER BY revision DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                if row is not None:
                    payload = json.loads(row["payload"])
                    if not isinstance(payload, dict):
                        raise ValueError("graph run payload must be an object")
                    if payload.get("status") not in {
                        "cancelled",
                        "completed",
                        "ready_to_promote",
                        "failed",
                    }:
                        generation = int(payload.get("generation", 0)) + 1
                        payload.update(
                            {
                                "generation": generation,
                                "status": "cancelled",
                                "failure_code": "GRAPH_CANCELLED",
                            }
                        )
                        connection.execute(
                            "INSERT OR REPLACE INTO records"
                            "(kind,record_id,run_id,revision,payload) VALUES(?,?,?,?,?)",
                            (
                                "graph_run_v2",
                                run_id,
                                run_id,
                                generation + 1,
                                canonical_json(payload),
                            ),
                        )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def control(self, run_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT action FROM controls WHERE run_id=?", (run_id,)
        ).fetchone()
        return None if row is None else str(row["action"])

    def clear_control(self, run_id: str) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM controls WHERE run_id=?", (run_id,))

    def save_work_run(self, run: Any) -> None:
        self.migrate_v2()
        self.put("work_run_v2", run, run_id=str(run.id), revision=int(run.generation) + 1)

    def get_work_run(self, run_id: str) -> Any:
        from .orchestration import WorkRun

        if self._schema_version() < 2:
            raise KeyError(run_id)
        return self.get("work_run_v2", run_id, WorkRun)

    def append_work_event(self, event: Any) -> int:
        self.migrate_v2()
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO work_events_v2(event_id, run_id, payload) VALUES(?,?,?)",
                (event.id, event.run_id, canonical_json(event)),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a v2 event sequence")
        return int(cursor.lastrowid)

    def work_events(self, run_id: str) -> tuple[Any, ...]:
        from .orchestration import WorkEvent

        if self._schema_version() < 2:
            return ()
        rows = self._connection.execute(
            "SELECT payload FROM work_events_v2 WHERE run_id=? ORDER BY sequence", (run_id,)
        )
        return tuple(WorkEvent.model_validate_json(row["payload"], strict=True) for row in rows)

    def checkpoint_work(self, run_id: str, generation: int, payload: object) -> None:
        self.migrate_v2()
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO work_checkpoints_v2(run_id,generation,payload) "
                "VALUES(?,?,?)",
                (run_id, generation, canonical_json(payload)),
            )

    def claim_graph_node(self, run_id: str, node_id: str, *, max_claims: int) -> bool:
        """Atomically reserve one unique node claim within the aggregate attempt cap."""

        if max_claims < 1:
            return False
        self.migrate_v2()
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO graph_claims_v2(run_id,node_id) "
                "SELECT ?,? WHERE "
                "(SELECT COUNT(*) FROM graph_claims_v2 WHERE run_id=?) < ?",
                (run_id, node_id, run_id, max_claims),
            )
        return cursor.rowcount == 1

    def reserve_graph_node(
        self,
        run_id: str,
        node_id: str,
        generation: int,
        attempt: int,
        *,
        max_claims: int,
        worker_turns: int,
        processes: int,
        wall_seconds: float,
        artifact_bytes: int,
        limits: dict[str, int | float],
        record_factory: Callable[[dict[str, int | float]], BaseModel],
    ) -> BaseModel | None:
        """Atomically claim one attempt, reserve all resources, and record the snapshot."""

        self.migrate_v2()
        requested = {
            "worker_turns": worker_turns,
            "processes": processes,
            "wall_seconds": wall_seconds,
            "artifact_bytes": artifact_bytes,
        }
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT 1 FROM graph_reservations_v2 "
                "WHERE run_id=? AND node_id=? AND generation=? AND attempt=?",
                (run_id, node_id, generation, attempt),
            ).fetchone()
            row = connection.execute(
                "SELECT COUNT(*) AS claims,"
                "COALESCE(SUM(worker_turns),0) AS worker_turns,"
                "COALESCE(SUM(processes),0) AS processes,"
                "COALESCE(SUM(wall_seconds),0) AS wall_seconds,"
                "COALESCE(SUM(artifact_bytes),0) AS artifact_bytes "
                "FROM graph_reservations_v2 WHERE run_id=?",
                (run_id,),
            ).fetchone()
            assert row is not None
            if (
                duplicate is not None
                or int(row["claims"]) >= max_claims
                or any(
                    float(row[name]) + float(value) > float(limits[name])
                    for name, value in requested.items()
                )
            ):
                connection.rollback()
                return None
            connection.execute(
                "INSERT INTO graph_reservations_v2"
                "(run_id,node_id,generation,attempt,worker_turns,processes,"
                "wall_seconds,artifact_bytes) VALUES(?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    node_id,
                    generation,
                    attempt,
                    worker_turns,
                    processes,
                    wall_seconds,
                    artifact_bytes,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO graph_claims_v2(run_id,node_id) VALUES(?,?)",
                (run_id, node_id),
            )
            remaining: dict[str, int | float] = {
                "node_attempts": max_claims - int(row["claims"]) - 1,
            }
            for name, value in requested.items():
                remaining[name] = limits[name] - float(row[name]) - float(value)
                if name != "wall_seconds":
                    remaining[name] = int(remaining[name])
            record = record_factory(remaining)
            record_id = getattr(record, "id", None)
            if record_id is None:
                raise ValueError("reservation record requires an id")
            connection.execute(
                "INSERT INTO records(kind,record_id,run_id,revision,payload) "
                "VALUES('node_reservation_v2',?,?,1,?)",
                (record_id, run_id, canonical_json(record)),
            )
            connection.commit()
            return record
        except BaseException:
            connection.rollback()
            raise

    def graph_claims(self, run_id: str) -> tuple[str, ...]:
        if self._schema_version() < 2:
            return ()
        rows = self._connection.execute(
            "SELECT node_id FROM graph_claims_v2 WHERE run_id=? ORDER BY node_id", (run_id,)
        )
        return tuple(str(row[0]) for row in rows)

    def load_work_checkpoint(self, run_id: str) -> tuple[int, dict[str, Any]]:
        if self._schema_version() < 2:
            raise KeyError(run_id)
        row = self._connection.execute(
            "SELECT generation,payload FROM work_checkpoints_v2 WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        payload = json.loads(row["payload"])
        if not isinstance(payload, dict):
            raise ValueError("v2 checkpoint payload must be an object")
        return int(row["generation"]), payload


class _TransitionRecord(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}
    id: str
    sequence: int
    transition: StateTransition

    @classmethod
    def from_transition(cls, sequence: int, transition: StateTransition) -> _TransitionRecord:
        return cls(
            id=f"{transition.entity_id}:{sequence}", sequence=sequence, transition=transition
        )
