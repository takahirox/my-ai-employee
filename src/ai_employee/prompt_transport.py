"""Model transport formatting, separate from authoritative record serialization."""

from collections.abc import Mapping

from .serialization import canonical_json


def prompt_json(payload: Mapping[str, object]) -> str:
    """Keep useful stable instructions before volatile data without changing fields."""

    stable = (
        "protocol",
        "instruction",
        "instructions",
        "response_contract",
        "transport_instruction",
        "response_schema",
        "rubric",
        "categorical_rubric",
    )
    keys = [key for key in stable if key in payload]
    keys.extend(sorted(set(payload) - set(keys)))
    return (
        "{"
        + ",".join(canonical_json(key) + ":" + canonical_json(payload[key]) for key in keys)
        + "}"
    )
