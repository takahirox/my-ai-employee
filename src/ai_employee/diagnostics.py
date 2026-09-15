"""Private, bounded diagnostic text; never an execution or acceptance input."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

RECORD_BYTES = 1_000_000
RUN_BYTES = 16_000_000
FAILURE_STREAM_BYTES = 32_768
_SECRET_KEY = re.compile(
    r"(?i)^(?:authorization|cookie|set-cookie|password|passwd|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|token|client[_-]?secret|private[_-]?key)$"
)
_PATTERNS = (
    re.compile(
        r"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----[\s\S]*?-----END (?:[A-Z ]*PRIVATE KEY)-----"
    ),
    re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,})\b"),
    re.compile(
        r"""(?i)\b(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|"""
        r"""client[_-]?secret|id[_-]?token|token|authorization|cookie)["']?\s*[=:]\s*"""
        r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
    ),
    re.compile(
        r"""(?i)\b(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|id[_-]?token|token|authorization|cookie)["']?\s*[=:]\s*["']?[^\s"',;]+"""
    ),
)


def redact(value: Any) -> tuple[Any, int]:
    """Redact complete values before a caller truncates their text or structure."""
    redactions = 0

    def text(value: str) -> str:
        nonlocal redactions
        for pattern in _PATTERNS:
            value, count = pattern.subn("[REDACTED]", value)
            redactions += count
        return value

    def clean(value: Any) -> Any:
        nonlocal redactions
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if _SECRET_KEY.fullmatch(str(key)) and item is not None:
                    result[text(str(key))] = "[REDACTED]"
                    redactions += 1
                else:
                    result[text(str(key))] = clean(item)
            return result
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        return text(value) if isinstance(value, str) else value

    result = clean(value)
    return result, redactions


def capture(value: Any, limit: int = RECORD_BYTES) -> dict[str, Any]:
    """Keep complete JSON/text within the cap; expose redaction and truncation."""
    original = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    cleaned, redactions = redact(value)
    sanitized = json.dumps(cleaned, ensure_ascii=False, sort_keys=True).encode()
    content = sanitized[: max(0, limit)].decode("utf-8", errors="ignore")
    return {
        "text": content,
        "format": "json",
        "original_bytes": len(original),
        "stored_bytes": len(content.encode()),
        "redactions": redactions,
        "truncated": len(sanitized) > max(0, limit),
        "sha256": hashlib.sha256(original).hexdigest(),
    }


def execution_snapshot(
    stdout: bytes | bytearray,
    stderr: bytes | bytearray,
    *,
    started: bool,
    exit_code: int | None = None,
    last_event: dict[str, Any] | None = None,
    last_update_seconds: float | None = None,
) -> dict[str, Any]:
    """Observed native output only; redact whole buffered values before taking tails.

    Input buffers already have the transport's hard output cap. No new I/O,
    credentials, command argv, host files or process introspection is performed.
    The exit code is the transport's code until guarded accounting identifies the
    native root exit. Neither this snapshot nor its text can decide control flow.
    """

    def stream(data: bytes | bytearray) -> dict[str, Any]:
        cleaned, redactions = redact(data.decode("utf-8", errors="replace"))
        encoded = cleaned.encode("utf-8")
        return {
            "tail": encoded[-FAILURE_STREAM_BYTES:].decode("utf-8", errors="ignore"),
            "observed_bytes": len(data),
            "redactions": redactions,
            "truncated": len(encoded) > FAILURE_STREAM_BYTES,
        }

    return {
        "started": started,
        "transport_exit_code": exit_code,
        "native_exit_code": None,
        "stdout": stream(stdout),
        "stderr": stream(stderr),
        "last_event": capture(last_event, 4096) if last_event is not None else None,
        "last_update_seconds": last_update_seconds,
        "network": "unknown; no additional collection after failure",
    }


def attach_failure(error: BaseException, snapshot: dict[str, Any]) -> None:
    """Carry bounded diagnostic data without wrapping/changing the control exception."""
    # Diagnostic failure must not replace timeout/cancellation/cleanup semantics.
    with suppress(Exception):
        error.fleet_execution_diagnostic = snapshot  # type: ignore[attr-defined]


def failure_snapshots(
    error: BaseException, *, boundary: BaseException | None = None
) -> dict[str, Any]:
    """Follow explicit and cleanup exception chains without their potentially secret text."""
    failures: list[dict[str, Any]] = []
    seen: set[int] = set()
    while error is not boundary and id(error) not in seen and len(failures) < 8:
        seen.add(id(error))
        snapshot = getattr(error, "fleet_execution_diagnostic", None)
        failures.append({"error_type": type(error).__name__, "execution": snapshot})
        parent = error.__cause__ or error.__context__
        if parent is None:
            break
        error = parent
    return {
        "failures": failures,
        "chain_truncated": error is not boundary and id(error) not in seen,
        "meaning": "diagnostic only; absent observations are unknown",
    }


@dataclass(frozen=True)
class CheckOutput:
    exit_code: int
    stdout: bytes
    stderr: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.stdout + self.stderr).hexdigest()

    def payload(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout.decode("utf-8", errors="replace"),
            "stderr": self.stderr.decode("utf-8", errors="replace"),
            "stdout_bytes": len(self.stdout),
            "stderr_bytes": len(self.stderr),
            "encoding": "utf-8; invalid bytes replaced",
        }
