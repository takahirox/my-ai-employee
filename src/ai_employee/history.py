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

from .models import Authority, RunConfig, Usage
from .stage_contracts import VERSION

# These events advance execution authority. Diagnostic observations and usage may
# still arrive after cancellation, but none of these decisions may do so.
DECISIONS = frozenset(
    {
        "goal",
        "plan",
        "accepted",
        "completed",
        "stage_result",
        "attempt_started",
        "worker_selected",
        "worker_result",
        "candidate",
        "verification",
        "authority_applied",
        "authority_approved",
        "clarification_wait",
        "readiness",
        "preflight",
    }
)


class Stopped(RuntimeError):
    """Control-plane stop: never feed into model retry/escalation."""


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        if path.is_symlink():
            raise ValueError("JOURNAL_SYMLINK")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
                "CREATE TABLE IF NOT EXISTS resource_leases (run TEXT NOT NULL, "
                "task TEXT NOT NULL, "
                "authority TEXT NOT NULL, PRIMARY KEY(run,task), "
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

    def _create(self, db: sqlite3.Connection, original: str, config: RunConfig) -> str:
        run = "run-" + uuid4().hex
        db.execute(
            "INSERT INTO runs VALUES(?,?,?,?)", (run, config.canonical(), original, time.time())
        )
        self._event(
            db,
            run,
            "created",
            {
                "config_digest": config.digest,
                "original_digest": hashlib.sha256(original.encode()).hexdigest(),
                "contract_version": VERSION,
            },
        )
        return run

    def create(self, original: str, config: RunConfig) -> str:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._create(db, original, config)

    def supersede(self, run: str, original: str, tree: str) -> str:
        config = self.config(run)
        prior_digest = hashlib.sha256(self.original(run).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM events WHERE run=? AND kind='stopped'", (run,)).fetchone():
                raise Stopped("TERMINAL_RUN_CANNOT_BE_REVISED")
            successor = self._create(db, original, config)
            self._event(db, successor, "input", {"tree": tree})
            self._event(
                db,
                successor,
                "goal_version",
                {
                    "previous_run": run,
                    "previous_original_digest": prior_digest,
                    "authorization": "explicit_human_replacement",
                },
            )
            self._event(db, run, "goal_superseded", {"successor": successor})
            self._event(db, run, "stopped", {"reason": "GOAL_SUPERSEDED"})
            return successor

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
            if kind in DECISIONS:
                self._decision_guard(db, run)
                if kind in {"accepted", "completed"}:
                    prior = db.execute(
                        "SELECT body FROM events WHERE run=? AND kind=?", (run, kind)
                    ).fetchall()
                    if any(json.loads(row[0]) == body for row in prior):
                        return
            self._event(db, run, kind, body)

    @staticmethod
    def _decision_guard(db: sqlite3.Connection, run: str) -> None:
        if db.execute(
            "SELECT 1 FROM events WHERE run=? "
            "AND kind IN ('stopped','authority_revocation_requested')",
            (run,),
        ).fetchone():
            raise Stopped("RUN_STOPPED")
        row = db.execute(
            "SELECT body FROM events WHERE run=? AND kind='created'", (run,)
        ).fetchone()
        if row is None or json.loads(row[0]).get("contract_version") != VERSION:
            raise Stopped("CONTRACT_VERSION_CHANGED")

    def append_many(self, run: str, entries: tuple[tuple[str, dict[str, Any]], ...]) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._decision_guard(db, run)
            for kind, body in entries:
                if kind in {"accepted", "completed"}:
                    prior = db.execute(
                        "SELECT body FROM events WHERE run=? AND kind=?", (run, kind)
                    ).fetchall()
                    if any(json.loads(row[0]) == body for row in prior):
                        continue
                self._event(db, run, kind, body)

    def budget(self, run: str) -> dict[str, Any]:
        limits = self.config(run).limits
        events = self.events(run)
        measured = {
            event["body"]["id"]: event["body"]["usage"]
            for event in events
            if event["kind"] == "settled"
        }
        with self.connect() as db:
            rows = db.execute("SELECT * FROM reservations WHERE run=?", (run,)).fetchall()
        known = {
            key: all(row["id"] in measured and measured[row["id"]][key] is not None for row in rows)
            for key in ("tokens", "cost")
        }
        return {
            "limits": limits.model_dump(mode="json"),
            "invocations": len(rows),
            "open_reservations": sum(not row["settled"] for row in rows),
            "active_seconds_charged": sum(row["seconds"] for row in rows),
            "admission_charges": {key: sum(row[key] for row in rows) for key in ("tokens", "cost")},
            "measured_usage": {
                key: sum(row[key] for row in rows) if known[key] else None
                for key in ("tokens", "cost")
            },
        }

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

    def runs(self, limit: int = 100) -> tuple[str, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("INVALID_HISTORY_LIMIT")
        with self.connect() as db:
            rows = db.execute(
                "SELECT id FROM runs ORDER BY created DESC LIMIT ?", (limit,)
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def check(self, run: str) -> None:
        config = self.config(run)
        events = self.events(run)
        if any(event["kind"] == "stopped" for event in events):
            raise Stopped("RUN_STOPPED")
        waiting = 0.0
        began: float | None = None
        pending: set[str] = set()
        for event in events:
            if event["kind"] == "approval_wait":
                if not pending:
                    began = event["at"]
                pending.add(event["body"]["attempt"])
            elif event["kind"] in {"authority_applied", "authority_rejected"}:
                pending.discard(event["body"]["attempt"])
                if not pending and began is not None:
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

    def reserve(
        self,
        run: str,
        stage: str,
        *,
        model_usage: bool = True,
        binding: dict[str, Any] | None = None,
        call_key: str | None = None,
        call_limit: int | None = None,
    ) -> tuple[str, float]:
        self.check(run)
        limits = self.config(run).limits
        reservation = "attempt-" + uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._decision_guard(db, run)
            ordinal = 0
            if call_key is not None:
                rows = db.execute(
                    "SELECT body FROM events WHERE run=? AND kind='reserved'", (run,)
                ).fetchall()
                ordinal = sum(json.loads(row[0]).get("call_key") == call_key for row in rows)
                if call_limit is not None and ordinal >= call_limit:
                    raise Stopped("STAGE_INVOCATION_LIMIT")
            usage = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(seconds),0),COALESCE(SUM(tokens),0),"
                "COALESCE(SUM(cost),0) FROM reservations WHERE run=?",
                (run,),
            ).fetchone()
            seconds = min(limits.invocation_seconds, limits.active_seconds - usage[1])
            tokens = limits.reservation_tokens if model_usage and limits.tokens is not None else 0
            cost = limits.reservation_cost if model_usage and limits.cost is not None else 0.0
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
                    "call_key": call_key,
                    "ordinal": ordinal,
                    "binding": binding,
                    "start_intent": True,
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
            if row is None:
                raise ValueError("FOREIGN_OR_SETTLED_RESERVATION")
            if row["settled"]:
                prior = db.execute(
                    "SELECT body FROM events WHERE run=? AND kind='settled'", (run,)
                ).fetchall()
                expected = {
                    "id": reservation,
                    "seconds": seconds,
                    "usage": usage.model_dump(mode="json"),
                }
                if any(json.loads(item[0]) == expected for item in prior):
                    return
                raise ValueError("CONFLICTING_USAGE_DELIVERY")
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

    def acquire_resources(self, run: str, task: str, authority: Authority) -> bool:
        """Serialize overlapping coarse write grants, including across Runs and restarts.

        A failed/unknown external attempt deliberately retains its lease until
        operator reconciliation; an interrupted controller is not proof of release.
        """
        if not authority.external_writes:
            return True
        import fnmatch

        def overlaps(other: Authority) -> bool:
            if not authority.network_hosts or not other.network_hosts:
                return True
            return any(
                fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a)
                for a in authority.network_hosts
                for b in other.network_hosts
            )

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM resource_leases").fetchall()
            if any(
                (row["run"], row["task"]) != (run, task)
                and overlaps(Authority.model_validate_json(row["authority"]))
                for row in rows
            ):
                self._event(db, run, "resource_wait", {"task_digest": task})
                return False
            db.execute(
                "INSERT OR REPLACE INTO resource_leases VALUES(?,?,?)",
                (run, task, authority.canonical()),
            )
            self._event(
                db,
                run,
                "resource_acquired",
                {"task_digest": task, "authority": authority.model_dump(mode="json")},
            )
            return True

    def release_resources(self, run: str, task: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("DELETE FROM resource_leases WHERE run=? AND task=?", (run, task))
            if cursor.rowcount:
                self._event(db, run, "resource_released", {"task_digest": task})

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
