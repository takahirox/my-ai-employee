"""Model transport formatting, separate from authoritative record serialization."""

from collections.abc import Mapping

from .engineering_guidance import configured_instruction, configured_rubric
from .serialization import canonical_json


def prompt_json(payload: Mapping[str, object]) -> str:
    """Keep useful stable instructions before volatile data without changing fields."""

    payload = dict(payload)
    protocol = payload.get("protocol")
    if isinstance(protocol, str) and protocol in {
        "fleet-proposed-graph/2",
        "fleet-worker-proposal/2",
        "fleet-isolated-candidate/1",
        "fleet-plan-review/2",
        "fleet-task-result-review/2",
        "fleet-parent-semantic-review/2",
    }:
        for key in ("instruction", "instructions"):
            instruction = payload.get(key)
            if isinstance(instruction, str):
                payload[key] = configured_instruction(instruction)
        rubric = payload.get("rubric")
        if isinstance(rubric, Mapping):
            payload["rubric"] = configured_rubric(rubric)
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
