"""Role-scoped deterministic context compilation and reference resolution."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from .domain import ContextPackage, ContextPolicy, ContextRole, Reference
from .domain.base import freeze_json
from .serialization import canonical_digest, canonical_json_bytes

ROLE_DEFAULTS: Mapping[ContextRole, ContextPolicy] = {
    ContextRole.PLANNER: ContextPolicy(
        id="context-planner",
        role=ContextRole.PLANNER,
        max_items=50,
        max_bytes=64_000,
        include_history=False,
    ),
    ContextRole.WORKER: ContextPolicy(
        id="context-worker",
        role=ContextRole.WORKER,
        max_items=20,
        max_bytes=96_000,
        include_history=False,
    ),
    ContextRole.VERIFIER: ContextPolicy(
        id="context-verifier",
        role=ContextRole.VERIFIER,
        max_items=30,
        max_bytes=96_000,
        include_history=False,
        allowed_reference_kinds=("document", "artifact", "evidence"),
    ),
    ContextRole.REVIEWER: ContextPolicy(
        id="context-reviewer",
        role=ContextRole.REVIEWER,
        max_items=40,
        max_bytes=128_000,
        include_history=False,
        allowed_reference_kinds=("artifact", "evidence", "document"),
    ),
}


class ContextCompiler:
    """Compile context from explicit authoritative sources, never chat history by default."""

    def compile(
        self,
        *,
        package_id: str,
        run_id: str,
        role: ContextRole,
        sources: Mapping[str, object],
        references: tuple[Reference, ...],
        policy: ContextPolicy | None = None,
    ) -> ContextPackage:
        selected_policy = policy or ROLE_DEFAULTS[role]
        if selected_policy.role is not role:
            raise ValueError("context policy role does not match requested role")
        allowed = set(selected_policy.allowed_reference_kinds)
        eligible = tuple(
            sorted(
                (item for item in references if item.kind in allowed),
                key=lambda item: (item.kind, item.target_id),
            )
        )
        selected: list[Reference] = []
        omitted: list[Reference] = []
        inline: dict[str, object] = {}
        used_bytes = 0
        for reference in eligible:
            value = sources.get(reference.target_id)
            size = len(canonical_json_bytes(value)) if value is not None else 0
            if (
                len(selected) >= selected_policy.max_items
                or used_bytes + size > selected_policy.max_bytes
            ):
                omitted.append(reference)
                continue
            selected.append(reference)
            used_bytes += size
            if value is not None and not selected_policy.pull_on_demand:
                inline[reference.target_id] = value
        digest_input = {
            "run_id": run_id,
            "role": role.value,
            "policy": selected_policy,
            "references": selected,
            "sources": {item.target_id: sources.get(item.target_id) for item in selected},
        }
        return ContextPackage(
            id=package_id,
            run_id=run_id,
            role=role,
            policy_id=selected_policy.id,
            compiled_at=datetime.now(UTC),
            authoritative_refs=tuple(selected),
            inline_items=freeze_json(inline),
            omitted_refs=tuple(omitted),
            source_digest=canonical_digest(digest_input),
        )

    @staticmethod
    def resolve(reference: Reference, sources: Mapping[str, object]) -> object:
        """Pull one referenced object on demand and verify its optional digest."""

        if reference.target_id not in sources:
            raise KeyError(reference.target_id)
        value = sources[reference.target_id]
        if reference.digest is not None and canonical_digest(value) != reference.digest:
            raise ValueError("referenced source digest does not match")
        return value


def substitute_references(value: object, sources: Mapping[str, object]) -> object:
    """Resolve only explicit ``{"$ref": "id"}`` markers in canonical data."""

    if isinstance(value, dict):
        if set(value) == {"$ref"} and isinstance(value["$ref"], str):
            return sources[value["$ref"]]
        return {key: substitute_references(item, sources) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(substitute_references(item, sources) for item in value)
    return value
