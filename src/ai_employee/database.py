"""Database path selection shared by normal Fleet CLI commands."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

DEFAULT_DATABASE_PATH = "~/.fleet/fleet.db"
DATABASE_ENVIRONMENT_VARIABLE = "FLEET_DB"


def resolve_database_path(
    explicit: str | Path | None,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve explicit CLI, environment, then user-level default database paths."""

    values = os.environ if environment is None else environment
    selected: str | Path | None = explicit
    if selected is None:
        selected = values.get(DATABASE_ENVIRONMENT_VARIABLE, DEFAULT_DATABASE_PATH)
    if not str(selected).strip():
        raise ValueError("Fleet database path must not be empty")
    return Path(selected).expanduser()
