"""Runtime-owned source fragments; models select IDs, never reproduce quotations."""

from __future__ import annotations

import hashlib
from itertools import pairwise

MAX_SOURCE_FRAGMENTS = 128
SOURCE_REFERENCE_RULE = (
    "Select one or more IDs from original_source.fragments, in source order without "
    "duplicates, including non-whitespace source text. IDs resolve only within the current "
    "Original Input binding. Never copy "
    "or rewrite quotation text. Runtime derives exact fragments, preserving whitespace "
    "and Unicode. Separate fragments need not be contiguous. A valid reference proves "
    "source location, not that it supports the proposed criterion or unresolved need."
)


def source_fragments(original: str) -> dict[str, str]:
    """Bound the reference set without losing any original characters or line endings."""
    lines = original.splitlines(keepends=True)
    width = max(1, (len(lines) + MAX_SOURCE_FRAGMENTS - 1) // MAX_SOURCE_FRAGMENTS)
    return {
        f"s{index // width + 1}": "".join(lines[index : index + width])
        for index in range(0, len(lines), width)
    }


def original_source(original: str) -> dict[str, object]:
    return {
        "digest": hashlib.sha256(original.encode()).hexdigest(),
        "fragments": source_fragments(original),
        "rule": SOURCE_REFERENCE_RULE,
    }


def resolve_source_refs(original: str, refs: tuple[str, ...]) -> tuple[str, ...]:
    fragments = source_fragments(original)
    order = {key: index for index, key in enumerate(fragments)}
    if (
        not refs
        or any(ref not in order for ref in refs)
        or any(order[left] >= order[right] for left, right in pairwise(refs))
        or not any(fragments[ref].strip() for ref in refs)
    ):
        raise ValueError("INVALID_ORIGINAL_REFERENCES")
    return tuple(fragments[ref] for ref in refs)
