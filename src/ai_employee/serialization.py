"""Canonical JSON, JSON-compatible YAML, digest, and typed loading helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import TypeVar

from pydantic import BaseModel

from .domain.base import ensure_utc, thaw_json

ModelT = TypeVar("ModelT", bound=BaseModel)


def _json_ready(value: object) -> object:
    if isinstance(value, BaseModel):
        return _json_ready(value.model_dump(mode="python", by_alias=True, exclude_none=False))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        normalized = ensure_utc(value)
        return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return thaw_json(value)


def canonical_json(value: object) -> str:
    """Encode a value using stable keys, separators, Unicode, and finite numbers."""

    return json.dumps(
        _json_ready(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_json_bytes(value: object) -> bytes:
    return canonical_json(value).encode("utf-8")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def project_harness_digest(harness: BaseModel) -> str:
    """Digest Harness v2 while preserving disabled review-field compatibility."""

    payload = harness.model_dump(mode="python", by_alias=True, exclude_none=False)
    verification = payload.get("verification")
    if isinstance(verification, dict):
        review = verification.get("review")
        if isinstance(review, dict) and review.get("independent_task_review") is False:
            review.pop("independent_task_review")
        if isinstance(review, dict) and review.get("parent_semantic_review") is False:
            review.pop("parent_semantic_review")
    return canonical_digest(payload)


def operator_config_digest(config: BaseModel) -> str:
    """Digest operator config without serializing disabled review opt-in defaults."""

    payload = config.model_dump(mode="python", by_alias=True, exclude_none=False)
    promotion = payload.get("promotion_auto_approval")
    if isinstance(promotion, dict) and promotion.get("mode") == "manual":
        payload.pop("promotion_auto_approval")
    routing = payload.get("routing")
    if isinstance(routing, dict):
        if routing.get("default_task_reviewer_strategy") is None:
            routing.pop("default_task_reviewer_strategy")
        if routing.get("default_parent_reviewer_strategy") is None:
            routing.pop("default_parent_reviewer_strategy")
        strategies = routing.get("strategies")
        if isinstance(strategies, (tuple, list)):
            for strategy in strategies:
                if isinstance(strategy, dict) and strategy.get("task_reviewer_eligible") is False:
                    strategy.pop("task_reviewer_eligible")
                if isinstance(strategy, dict) and strategy.get("parent_reviewer_eligible") is False:
                    strategy.pop("parent_reviewer_eligible")
    return canonical_digest(payload)


DIGEST_ALGORITHM = "sha256"
DIGEST_FORMAT_VERSION = "1"


def canonical_content(value: object, *, excluded_fields: frozenset[str] = frozenset()) -> object:
    """Return the deterministic content payload used by v2 identity digests.

    Callers explicitly select identity-only fields (timestamps, stored digests, and
    secret bindings) for exclusion.  Explicit null values in the remaining payload
    are retained.
    """

    ready = _json_ready(value)
    return _exclude_fields(ready, excluded_fields)


def _exclude_fields(value: object, excluded_fields: frozenset[str]) -> object:
    if isinstance(value, Mapping):
        return {
            key: _exclude_fields(item, excluded_fields)
            for key, item in value.items()
            if key not in excluded_fields
        }
    if isinstance(value, list):
        return [_exclude_fields(item, excluded_fields) for item in value]
    return value


def versioned_digest(
    value: object,
    *,
    excluded_fields: frozenset[str] = frozenset(),
    algorithm: str = DIGEST_ALGORITHM,
    format_version: str = DIGEST_FORMAT_VERSION,
) -> str:
    """Digest a versioned canonical envelope, failing closed on unknown formats."""

    if algorithm != DIGEST_ALGORITHM:
        raise ValueError(f"unsupported digest algorithm: {algorithm}")
    if format_version != DIGEST_FORMAT_VERSION:
        raise ValueError(f"unsupported digest format version: {format_version}")
    envelope = {
        "algorithm": algorithm,
        "format_version": format_version,
        "payload": canonical_content(value, excluded_fields=excluded_fields),
    }
    return canonical_digest(envelope)


def dumps_yaml(value: object) -> str:
    """Emit deterministic JSON, which is a portable YAML 1.2 representation."""

    return (
        json.dumps(
            _json_ready(value),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


def loads_model(text: str | bytes, model_type: type[ModelT]) -> ModelT:
    """Strictly parse canonical JSON into a declared model type."""

    return model_type.model_validate_json(text, strict=True)


def loads_yaml_model(text: str, model_type: type[ModelT]) -> ModelT:
    """Parse safe YAML and strictly validate it as the declared model type."""

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - dependency exists in installations
            raise RuntimeError("PyYAML is required for non-JSON YAML input") from exc
        data = yaml.safe_load(text)
    # Re-enter through JSON so YAML sequences retain normal wire-array semantics
    # while model-level strictness still rejects coercion and unknown fields.
    wire_json = json.dumps(_json_ready(data), ensure_ascii=False, allow_nan=False)
    return model_type.model_validate_json(wire_json, strict=True)
