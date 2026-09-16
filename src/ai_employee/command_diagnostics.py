"""Bounded command snapshots, separate from authority-bearing append-only events."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .diagnostics import redact


class CommandCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = True
    command_bytes: int = Field(default=65_536, ge=4096, le=1_000_000)
    run_bytes: int = Field(default=4_000_000, ge=4096, le=64_000_000)
    max_commands: int = Field(default=1000, ge=1, le=10_000)
    retention_days: int = Field(default=7, ge=1, le=365)

    @model_validator(mode="after")
    def fits_run(self) -> Self:
        if self.command_bytes > self.run_bytes:
            raise ValueError("COMMAND_CAPTURE_EXCEEDS_RUN_CAP")
        return self


FIELDS = ("command", "cwd", "stdout", "stderr", "combined_output")


def command_event(event: dict[str, Any], elapsed: float) -> dict[str, Any] | None:
    """Allowlist command snapshots, never messages, reasoning or arbitrary event fields.

    Redaction happens here before any payload truncation and again at persistence.
    Missing IDs cannot be safely correlated and are explicitly reported as unavailable.
    Codex exec reports aggregated_output, not distinct stdout and stderr.
    """
    kind = event.get("type")
    item = event.get("item")
    if kind not in {"item.started", "item.updated", "item.completed"} or not isinstance(item, dict):
        return None
    if item.get("type") != "command_execution":
        return None
    identifier = item.get("id")
    key = (
        hashlib.sha256(identifier.encode()).hexdigest()
        if isinstance(identifier, str) and identifier
        else None
    )
    data: dict[str, Any] = {
        "event": "command_snapshot",
        "key": key,
        "item_id": identifier,
        "phase": kind.removeprefix("item."),
        "observed_at": time.time(),
        "observed_elapsed": elapsed,
    }
    # Provider statuses are not execution authority. Whitelist before storage.
    data["status"] = (
        item.get("status") if item.get("status") in {"in_progress", "completed", "failed"} else None
    )
    data["exit_code"] = item.get("exit_code") if type(item.get("exit_code")) is int else None
    duration = item.get("duration_ms")
    data["provider_duration_seconds"] = (
        duration / 1000
        if isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and 0 <= duration <= 31_536_000_000
        and math.isfinite(duration)
        else None
    )
    for name in FIELDS:
        value = item.get("aggregated_output" if name == "combined_output" else name)
        if isinstance(value, str):
            cleaned, count = redact(value)
            data[name] = {
                "text": cleaned,
                "redactions": count,
                "observed_bytes": len(value.encode()),
            }
    # IDs can also contain secrets; hashes provide correlation without retaining the raw ID.
    if isinstance(identifier, str):
        cleaned_id, count = redact(identifier)
        data["item_id"] = {
            "text": cleaned_id,
            "redactions": count,
            "observed_bytes": len(identifier.encode()),
        }
    else:
        data["item_id"] = None
    return data


def initialize(db: sqlite3.Connection) -> None:
    db.executescript(
        "CREATE TABLE IF NOT EXISTS command_records ("
        "run TEXT NOT NULL, reservation TEXT NOT NULL, command_key TEXT NOT NULL,"
        "stage TEXT NOT NULL, expires REAL NOT NULL, body TEXT NOT NULL,"
        "removed TEXT, PRIMARY KEY(run,reservation,command_key),"
        "FOREIGN KEY(run) REFERENCES runs(id));"
        "CREATE TABLE IF NOT EXISTS command_capture_state ("
        "run TEXT PRIMARY KEY, omitted_events INTEGER NOT NULL DEFAULT 0,"
        "purged INTEGER NOT NULL DEFAULT 0, missing_ids INTEGER NOT NULL DEFAULT 0, "
        "count_limited INTEGER NOT NULL DEFAULT 0, byte_limited INTEGER NOT NULL DEFAULT 0, "
        "FOREIGN KEY(run) REFERENCES runs(id));"
    )


def expire(db: sqlite3.Connection, now: float) -> None:
    # Tombstones prevent late/duplicate snapshots from resurrecting expired content.
    db.execute(
        "UPDATE command_records SET body='{}', removed='expired' "
        "WHERE removed IS NULL AND expires<=?",
        (now,),
    )


def _encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _field(raw: Any, limit: int) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
        return {"text": None, "status": "unavailable", "reason": "not_reported_by_provider"}
    clean, count = redact(raw["text"])
    encoded = clean.encode()
    truncated = len(encoded) > limit
    return {
        "text": encoded[:limit].decode("utf-8", errors="ignore"),
        "status": "truncated" if truncated else "available",
        "reason": "command_byte_limit" if truncated else None,
        "redactions": count + int(raw.get("redactions", 0)),
        "observed_bytes": raw.get("observed_bytes"),
    }


def omit(db: sqlite3.Connection, run: str, reason: str) -> None:
    columns = {
        "missing_id": "missing_ids",
        "count_limit": "count_limited",
        "byte_limit": "byte_limited",
    }
    column = columns[reason]
    db.execute(
        f"UPDATE command_capture_state SET omitted_events=omitted_events+1, "
        f"{column}={column}+1 WHERE run=?",
        (run,),
    )


def record(
    db: sqlite3.Connection,
    run: str,
    reservation: str,
    stage: str,
    event: dict[str, Any],
    policy: CommandCapture,
    context: dict[str, Any],
) -> None:
    """Update one sanitized snapshot transactionally; observations never affect execution."""
    if not policy.enabled:
        return
    now = time.time()
    expire(db, now)
    db.execute("INSERT OR IGNORE INTO command_capture_state(run) VALUES(?)", (run,))
    state = db.execute("SELECT purged FROM command_capture_state WHERE run=?", (run,)).fetchone()
    if state[0]:
        return
    key = event.get("key")
    if not isinstance(key, str) or len(key) != 64:
        omit(db, run, "missing_id")
        return
    previous = db.execute(
        "SELECT body,removed FROM command_records WHERE run=? AND reservation=? AND command_key=?",
        (run, reservation, key),
    ).fetchone()
    if previous is not None and previous[1]:
        return
    if (
        previous is None
        and db.execute("SELECT count(*) FROM command_records WHERE run=?", (run,)).fetchone()[0]
        >= policy.max_commands
    ):
        omit(db, run, "count_limit")
        return
    old = json.loads(previous[0]) if previous else {}
    # Hash sanitized event content, excluding reception clocks, for idempotent replay.
    fingerprint = hashlib.sha256(
        _encoded(
            {k: v for k, v in event.items() if k not in {"observed_at", "observed_elapsed"}}
        ).encode()
    ).hexdigest()
    phase = event.get("phase")
    if old and (old.get("snapshot_digest") == fingerprint or old.get("completed")):
        return
    if phase == "started" and old.get("started_at") is not None:
        return
    safe_context, context_redactions = redact({k: context.get(k) for k in ("task", "attempt")})
    result = old or {
        "run": run,
        "reservation": reservation,
        "stage": stage,
        **safe_context,
        "context_redactions": context_redactions,
        "command_id": _field(event.get("item_id"), 256),
        "started_at": None,
        "finished_at": None,
        "observed_duration_seconds": None,
        "start_elapsed": None,
        "provider_duration_seconds": None,
        "provider_duration_source": "provider item.duration_ms when available",
        "timing_source": "controller event reception; may be buffered, not process runtime",
        "session_workspace": "/work",
        "completed": False,
        **{name: _field(None, 0) for name in FIELDS},
    }
    if phase == "started":
        result.update(started_at=event["observed_at"], start_elapsed=event["observed_elapsed"])
    if phase == "completed":
        result.update(finished_at=event["observed_at"], completed=True)
        if result["start_elapsed"] is not None:
            result["observed_duration_seconds"] = max(
                0, event["observed_elapsed"] - result["start_elapsed"]
            )
    result.update(
        status=event.get("status"), exit_code=event.get("exit_code"), snapshot_digest=fingerprint
    )
    if event.get("provider_duration_seconds") is not None:
        result["provider_duration_seconds"] = event["provider_duration_seconds"]
    for name in FIELDS:
        if name in event:
            result[name] = _field(event[name], policy.command_bytes // 10)
    result["output_format"] = (
        "combined"
        if result["combined_output"]["text"] is not None
        else "separate"
        if any(result[n]["text"] is not None for n in ("stdout", "stderr"))
        else "unavailable"
    )
    used = db.execute(
        "SELECT COALESCE(SUM(length(CAST(body AS BLOB))),0) FROM command_records WHERE run=?",
        (run,),
    ).fetchone()[0]
    allowed = min(
        policy.command_bytes,
        policy.run_bytes - used + (len(previous[0].encode()) if previous else 0),
    )
    body = _encoded(result)
    if len(body.encode()) > allowed:
        # Preserve correlation and exit information when content no longer fits.
        for name in FIELDS:
            if result[name]["text"] is not None:
                result[name].update(text="", status="truncated", reason="run_or_command_byte_limit")
        body = _encoded(result)
    if len(body.encode()) > allowed:
        omit(db, run, "byte_limit")
        return
    db.execute(
        "INSERT INTO command_records VALUES(?,?,?,?,?,?,NULL) "
        "ON CONFLICT(run,reservation,command_key) DO UPDATE SET body=excluded.body",
        (run, reservation, key, stage, now + policy.retention_days * 86400, body),
    )


def read(
    db: sqlite3.Connection,
    run: str,
    *,
    stage: str | None = None,
    reservation: str | None = None,
    failed_only: bool = False,
) -> dict[str, Any]:
    expire(db, time.time())
    records = []
    for row in db.execute("SELECT * FROM command_records WHERE run=? ORDER BY rowid", (run,)):
        if (stage is not None and row["stage"] != stage) or (
            reservation is not None and row["reservation"] != reservation
        ):
            continue
        if row["removed"]:
            if not failed_only:
                records.append(
                    {
                        "reservation": row["reservation"],
                        "stage": row["stage"],
                        "unavailable": row["removed"],
                    }
                )
            continue
        body = json.loads(row["body"])
        if failed_only and not (
            body.get("exit_code") not in (None, 0) or body.get("status") == "failed"
        ):
            continue
        settled = db.execute(
            "SELECT settled FROM reservations WHERE run=? AND id=?", (run, row["reservation"])
        ).fetchone()
        body["completion_observation"] = (
            "completed_event"
            if body["completed"]
            else "invocation_ended_without_command_completion"
            if settled and settled[0]
            else "completion_not_observed"
        )
        body.pop("start_elapsed", None)
        body.pop("snapshot_digest", None)
        records.append(body)
    state = db.execute("SELECT * FROM command_capture_state WHERE run=?", (run,)).fetchone()
    return {
        "authoritative": False,
        "records": records,
        "omitted_events": state["omitted_events"] if state else 0,
        "omission_reasons": {
            reason: state[column] if state else 0
            for reason, column in (
                ("missing_command_id", "missing_ids"),
                ("command_count_limit", "count_limited"),
                ("byte_limit", "byte_limited"),
            )
        },
        "purged": bool(state and state["purged"]),
    }


def purge(db: sqlite3.Connection, run: str) -> int:
    count = db.execute(
        "SELECT count(*) FROM command_records WHERE run=? AND removed IS NULL", (run,)
    ).fetchone()[0]
    db.execute("UPDATE command_records SET body='{}',removed='purged' WHERE run=?", (run,))
    db.execute(
        "INSERT INTO command_capture_state(run,purged) VALUES(?,1) "
        "ON CONFLICT(run) DO UPDATE SET purged=1",
        (run,),
    )
    return int(count)
