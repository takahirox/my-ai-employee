"""Private, bounded diagnostic text; never an execution or acceptance input."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

RECORD_BYTES = 1_000_000
RUN_BYTES = 16_000_000
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


def capture(value: Any, limit: int = RECORD_BYTES) -> dict[str, Any]:
    """Keep complete JSON/text within the cap; expose redaction and truncation."""
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

    original = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    sanitized = json.dumps(clean(value), ensure_ascii=False, sort_keys=True).encode()
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
