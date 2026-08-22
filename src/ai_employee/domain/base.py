"""Strict, immutable schema primitives shared by the trust kernel."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, ClassVar, TypeAlias

from pydantic import BeforeValidator, ConfigDict, Field, PlainSerializer, field_validator
from pydantic.main import BaseModel

IDENTIFIER_PATTERN = r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)

Identifier = Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]
Digest = Annotated[str, Field(pattern=DIGEST_PATTERN)]
UtcTimestamp = Annotated[datetime, BeforeValidator(lambda value: ensure_utc(value))]


class StableStrEnum(StrEnum):
    """String enum whose wire value is deliberately stable."""

    def __str__(self) -> str:
        return self.value


class FrozenDict(dict[str, Any]):
    """JSON object that retains normal serialization but rejects mutation."""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FrozenDict:
        result = cls()
        for key, item in value.items():
            dict.__setitem__(result, key, item)
        return result

    def _immutable(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("canonical JSON values are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple[Any, ...] | FrozenDict


def ensure_utc(value: object) -> datetime:
    """Require timezone-aware timestamps and normalize them to UTC."""

    if isinstance(value, str):
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(candidate)
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError("timestamp must be a datetime or ISO 8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def freeze_json(value: object) -> JsonValue:
    """Validate a JSON-compatible tree and return an immutable equivalent."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not valid canonical JSON")
        return value
    if isinstance(value, dict):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            frozen[key] = freeze_json(item)
        return FrozenDict.from_dict(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def thaw_json(value: object) -> object:
    """Convert an immutable canonical tree to ordinary JSON containers."""

    if isinstance(value, dict):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


CanonicalData = Annotated[
    JsonValue,
    BeforeValidator(freeze_json),
    PlainSerializer(thaw_json, return_type=object),
]


class SchemaModel(BaseModel):
    """Base for all authoritative, versioned wire models."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
        use_enum_values=False,
        arbitrary_types_allowed=True,
    )

    schema_version: str = Field(default="1", pattern=r"^1$")
    schema_name: ClassVar[str]

    @field_validator("schema_version")
    @classmethod
    def _supported_schema_version(cls, value: str) -> str:
        if value != "1":
            raise ValueError(f"unsupported {cls.__name__} schema version: {value}")
        return value


class EntityModel(SchemaModel):
    """A versioned domain object with a stable identifier."""

    id: Identifier
