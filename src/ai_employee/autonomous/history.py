"""Append-only, digest-checked execution journal and atomic shared reservations.

The new journal deliberately has no legacy migration. A process lock serializes
controllers; worker threads use independent SQLite connections and transactions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import RunConfig, Usage


class Stopped(RuntimeError):
    """Control-plane stop: never feed into model retry/escalation."""


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        if path.is_symlink():
            raise ValueError("JOURNAL_SYMLINK")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version not in (0, 167) or (version == 0 and tables):
                raise ValueError("UNSUPPORTED_HISTORY: use a new autonomous journal")
            db.executescript(
                "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, config TEXT NOT NULL, "
                "original TEXT NOT NULL, created REAL NOT NULL);"
                "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, run TEXT NOT NULL, "
                "kind TEXT NOT NULL, body TEXT NOT NULL, at REAL NOT NULL, previous TEXT NOT NULL, "
                "digest TEXT NOT NULL UNIQUE, FOREIGN KEY(run) REFERENCES runs(id));"
                "CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, run TEXT NOT NULL, "
                "stage TEXT NOT NULL, started REAL NOT NULL, seconds REAL NOT NULL, "
                "tokens INTEGER NOT NULL, cost REAL NOT NULL, settled INTEGER NOT NULL DEFAULT 0, "
                "FOREIGN KEY(run) REFERENCES runs(id));"
                "PRAGMA user_version=167;"
            )
        path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _event(db: sqlite3.Connection, run: str, kind: str, body: dict[str, Any]) -> None:
        last = db.execute(
            "SELECT digest FROM events WHERE run=? ORDER BY seq DESC LIMIT 1", (run,)
        ).fetchone()
        previous = "" if last is None else str(last[0])
        at = time.time()
        payload = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(
            json.dumps([run, kind, payload, at, previous], separators=(",", ":")).encode()
        ).hexdigest()
        db.execute(
            "INSERT INTO events(run,kind,body,at,previous,digest) VALUES(?,?,?,?,?,?)",
            (run, kind, payload, at, previous, digest),
        )

    def create(self, original: str, config: RunConfig) -> str:
        run = "run-" + uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?)",
                (run, config.canonical(), original, time.time()),
            )
            self._event(
                db,
                run,
                "created",
                {
                    "config_digest": config.digest,
                    "original_digest": hashlib.sha256(original.encode()).hexdigest(),
                },
            )
        return run

    def config(self, run: str) -> RunConfig:
        with self.connect() as db:
            row = db.execute("SELECT config FROM runs WHERE id=?", (run,)).fetchone()
        if row is None:
            raise KeyError(run)
        config = RunConfig.model_validate_json(row[0])
        events = self.events(run)
        if not events or events[0]["body"]["config_digest"] != config.digest:
            raise ValueError("RUN_CONFIG_CHANGED")
        return config

    def original(self, run: str) -> str:
        with self.connect() as db:
            row = db.execute("SELECT original FROM runs WHERE id=?", (run,)).fetchone()
        if row is None:
            raise KeyError(run)
        original = str(row[0])
        digest = hashlib.sha256(original.encode()).hexdigest()
        if self.events(run)[0]["body"]["original_digest"] != digest:
            raise ValueError("ORIGINAL_INPUT_CHANGED")
        return original

    def append(self, run: str, kind: str, **body: Any) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._event(db, run, kind, body)

    def events(self, run: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE run=? ORDER BY seq", (run,)).fetchall()
        previous = ""
        result: list[dict[str, Any]] = []
        for row in rows:
            expected = hashlib.sha256(
                json.dumps(
                    [run, row["kind"], row["body"], row["at"], previous], separators=(",", ":")
                ).encode()
            ).hexdigest()
            if row["previous"] != previous or row["digest"] != expected:
                raise ValueError("HISTORY_INTEGRITY_FAILURE")
            previous = expected
            result.append({"kind": row["kind"], "at": row["at"], "body": json.loads(row["body"])})
        return result

    def stop(self, run: str, reason: str) -> None:
        self.append(run, "stopped", reason=reason)

    def check(self, run: str) -> None:
        config = self.config(run)
        events = self.events(run)
        if any(event["kind"] == "stopped" for event in events):
            raise Stopped("RUN_STOPPED")
        waiting = 0.0
        began: float | None = None
        for event in events:
            if event["kind"] == "approval_wait" and began is None:
                began = event["at"]
            elif event["kind"] in {"authority_applied", "authority_rejected"} and began is not None:
                waiting += event["at"] - began
                began = None
        if began is not None:
            waiting += time.time() - began
        elapsed = time.time() - events[0]["at"]
        if not config.limits.approval_counts_wall:
            elapsed -= waiting
        if elapsed >= config.limits.wall_seconds:
            self.stop(run, "WALL_BUDGET_EXHAUSTED")
            raise Stopped("WALL_BUDGET_EXHAUSTED")
        with self.connect() as db:
            usage = db.execute(
                "SELECT COALESCE(SUM(seconds),0), COALESCE(SUM(tokens),0), "
                "COALESCE(SUM(cost),0) FROM reservations WHERE run=?",
                (run,),
            ).fetchone()
        limits = config.limits
        if (
            usage[0] > limits.active_seconds
            or (limits.tokens is not None and usage[1] > limits.tokens)
            or (limits.cost is not None and usage[2] > limits.cost)
        ):
            self.stop(run, "RUN_BUDGET_EXHAUSTED")
            raise Stopped("RUN_BUDGET_EXHAUSTED")

    def reserve(self, run: str, stage: str) -> tuple[str, float]:
        self.check(run)
        limits = self.config(run).limits
        reservation = "attempt-" + uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM events WHERE run=? AND kind='stopped'", (run,)).fetchone():
                raise Stopped("RUN_STOPPED")
            usage = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(seconds),0),COALESCE(SUM(tokens),0),"
                "COALESCE(SUM(cost),0) FROM reservations WHERE run=?",
                (run,),
            ).fetchone()
            seconds = min(limits.invocation_seconds, limits.active_seconds - usage[1])
            tokens = limits.reservation_tokens if limits.tokens is not None else 0
            cost = limits.reservation_cost if limits.cost is not None else 0.0
            if (
                usage[0] >= limits.attempts
                or seconds <= 0
                or (limits.tokens is not None and usage[2] + tokens > limits.tokens)
                or (limits.cost is not None and usage[3] + cost > limits.cost)
            ):
                raise Stopped("RUN_BUDGET_EXHAUSTED")
            db.execute(
                "INSERT INTO reservations(id,run,stage,started,seconds,tokens,cost) "
                "VALUES(?,?,?,?,?,?,?)",
                (reservation, run, stage, time.time(), seconds, tokens, cost),
            )
            self._event(
                db,
                run,
                "reserved",
                {
                    "id": reservation,
                    "stage": stage,
                    "seconds": seconds,
                    "tokens": tokens,
                    "cost": cost,
                },
            )
        return reservation, seconds

    def settle(self, run: str, reservation: str, seconds: float, usage: Usage) -> None:
        if seconds < 0:
            raise ValueError("INVALID_USAGE")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM reservations WHERE id=? AND run=?", (reservation, run)
            ).fetchone()
            if row is None or row["settled"]:
                raise ValueError("FOREIGN_OR_SETTLED_RESERVATION")
            # Missing data retains the full reservation, including after a crash.
            tokens = row["tokens"] if usage.tokens is None else usage.tokens
            cost = row["cost"] if usage.cost is None else usage.cost
            db.execute(
                "UPDATE reservations SET seconds=?,tokens=?,cost=?,settled=1 WHERE id=?",
                (seconds, tokens, cost, reservation),
            )
            self._event(
                db,
                run,
                "settled",
                {"id": reservation, "seconds": seconds, "usage": usage.model_dump(mode="json")},
            )

    @contextmanager
    def controller(self, run: str) -> Iterator[None]:
        import fcntl

        # Validate the runtime-generated ID before using it in a path.
        if not run.startswith("run-") or len(run) != 36 or not run[4:].isalnum():
            raise ValueError("INVALID_RUN_ID")
        lock = self.path.with_name(self.path.name + "." + run + ".lock")
        with lock.open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Stopped("RUN_ALREADY_OWNED") from error
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
