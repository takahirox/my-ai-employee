"""Five-layer, monotonically restrictive v2 policy resolution."""

from __future__ import annotations

from typing import ClassVar, Self

from pydantic import Field, field_validator, model_validator

from .base import Identifier, StableStrEnum, UtcTimestamp, freeze_json
from .v2 import (
    ActionProposal,
    DecisionOutcome,
    DigestedRecordV2,
    InstallRequest,
    PolicyDecision,
    SchemaModelV2,
)


class PolicyLayerKind(StableStrEnum):
    BUILTIN = "builtin"
    OPERATOR = "operator"
    PROJECT = "project"
    RUN = "run"
    WORKER = "worker"


POLICY_PRECEDENCE = (
    PolicyLayerKind.BUILTIN,
    PolicyLayerKind.OPERATOR,
    PolicyLayerKind.PROJECT,
    PolicyLayerKind.RUN,
    PolicyLayerKind.WORKER,
)


class NetworkMode(StableStrEnum):
    DISABLED = "disabled"
    RESTRICTED = "restricted"
    FULL = "full"


_NETWORK_AUTHORITY = {
    NetworkMode.DISABLED: 0,
    NetworkMode.RESTRICTED: 1,
    NetworkMode.FULL: 2,
}


class PolicyLayer(DigestedRecordV2):
    schema_name: ClassVar[str] = "policy_layer"
    kind: PolicyLayerKind
    allowed_capabilities: tuple[Identifier, ...] | None = None
    denied_capabilities: tuple[Identifier, ...] = ()
    writable_paths: tuple[str, ...] | None = None
    https_domains: tuple[str, ...] | None = None
    network_mode: NetworkMode | None = None
    process_shell_allowed: bool | None = None
    install_ecosystems: tuple[Identifier, ...] | None = None
    max_wall_seconds: float | None = Field(default=None, gt=0)
    max_processes: int | None = Field(default=None, ge=0)
    max_worker_turns: int | None = Field(default=None, ge=0)
    max_download_bytes: int | None = Field(default=None, ge=0)
    max_artifact_bytes: int | None = Field(default=None, ge=0)
    required_approvals: tuple[Identifier, ...] = ()

    @field_validator(
        "allowed_capabilities",
        "denied_capabilities",
        "writable_paths",
        "https_domains",
        "install_ecosystems",
        "required_approvals",
    )
    @classmethod
    def _unique_sets(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("policy set fields must not contain duplicates")
        return None if value is None else tuple(sorted(value))

    @model_validator(mode="after")
    def _no_allow_and_deny_conflict(self) -> Self:
        if self.allowed_capabilities is not None:
            conflict = set(self.allowed_capabilities) & set(self.denied_capabilities)
            if conflict:
                raise ValueError(
                    f"capabilities cannot be both allowed and denied: {sorted(conflict)}"
                )
        return self


class EffectivePolicy(DigestedRecordV2):
    schema_name: ClassVar[str] = "effective_policy"
    source_layer_digests: tuple[str, ...]
    allowed_capabilities: tuple[Identifier, ...] | None
    denied_capabilities: tuple[Identifier, ...]
    writable_paths: tuple[str, ...] | None
    https_domains: tuple[str, ...] | None
    network_mode: NetworkMode
    process_shell_allowed: bool
    install_ecosystems: tuple[Identifier, ...] | None
    max_wall_seconds: float
    max_processes: int
    max_worker_turns: int
    max_download_bytes: int
    max_artifact_bytes: int
    required_approvals: tuple[Identifier, ...]


class PolicyResolutionError(ValueError):
    """Stable fail-closed rejection of malformed or authority-increasing layers."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PolicyResolution(SchemaModelV2):
    schema_name: ClassVar[str] = "policy_resolution"
    effective_policy: EffectivePolicy
    decision: PolicyDecision


def _intersection(
    current: tuple[str, ...] | None, proposed: tuple[str, ...] | None
) -> tuple[str, ...] | None:
    if proposed is None:
        return current
    if current is None:
        return tuple(sorted(proposed))
    extra = set(proposed) - set(current)
    if extra:
        raise PolicyResolutionError(
            "POLICY_AUTHORITY_INCREASE",
            f"lower policy layer attempted to add allowlisted values: {sorted(extra)}",
        )
    return tuple(sorted(set(current) & set(proposed)))


class PolicyResolver:
    """Resolve built-in→operator→project→run→worker restrictions deterministically."""

    def resolve(
        self,
        request: ActionProposal,
        policy_layers: tuple[PolicyLayer, ...],
        *,
        decision_id: Identifier,
        created_at: UtcTimestamp,
    ) -> PolicyResolution:
        if not policy_layers or policy_layers[0].kind is not PolicyLayerKind.BUILTIN:
            raise PolicyResolutionError(
                "POLICY_LAYER_ORDER", "the built-in layer is required first"
            )
        builtin = policy_layers[0]
        required_builtin_fields = (
            "allowed_capabilities",
            "writable_paths",
            "https_domains",
            "network_mode",
            "process_shell_allowed",
            "install_ecosystems",
            "max_wall_seconds",
            "max_processes",
            "max_worker_turns",
            "max_download_bytes",
            "max_artifact_bytes",
        )
        if any(getattr(builtin, name) is None for name in required_builtin_fields):
            raise PolicyResolutionError(
                "POLICY_INCOMPLETE_BUILTIN", "the built-in policy floor must be explicit"
            )
        order = tuple(layer.kind for layer in policy_layers)
        expected = tuple(kind for kind in POLICY_PRECEDENCE if kind in order)
        if order != expected or len(set(order)) != len(order):
            raise PolicyResolutionError(
                "POLICY_LAYER_ORDER", "policy layers must be unique and in fixed precedence order"
            )

        allowed: tuple[str, ...] | None = tuple(sorted(builtin.allowed_capabilities or ()))
        denied: set[str] = set(builtin.denied_capabilities)
        paths: tuple[str, ...] | None = tuple(sorted(builtin.writable_paths or ()))
        domains: tuple[str, ...] | None = tuple(sorted(builtin.https_domains or ()))
        ecosystems: tuple[str, ...] | None = tuple(sorted(builtin.install_ecosystems or ()))
        network = builtin.network_mode
        shell = builtin.process_shell_allowed
        assert network is not None and shell is not None
        limits: dict[str, float | int] = {
            "max_wall_seconds": builtin.max_wall_seconds or 0,
            "max_processes": builtin.max_processes or 0,
            "max_worker_turns": builtin.max_worker_turns or 0,
            "max_download_bytes": builtin.max_download_bytes or 0,
            "max_artifact_bytes": builtin.max_artifact_bytes or 0,
        }
        approvals: set[str] = set(builtin.required_approvals)

        for layer in policy_layers[1:]:
            allowed = _intersection(allowed, layer.allowed_capabilities)
            paths = _intersection(paths, layer.writable_paths)
            domains = _intersection(domains, layer.https_domains)
            ecosystems = _intersection(ecosystems, layer.install_ecosystems)
            denied.update(layer.denied_capabilities)
            approvals.update(layer.required_approvals)
            if layer.network_mode is not None:
                if _NETWORK_AUTHORITY[layer.network_mode] > _NETWORK_AUTHORITY[network]:
                    raise PolicyResolutionError(
                        "POLICY_AUTHORITY_INCREASE",
                        f"{layer.kind.value} layer attempted to increase network authority",
                    )
                network = layer.network_mode
            if layer.process_shell_allowed is not None:
                if layer.process_shell_allowed and not shell:
                    raise PolicyResolutionError(
                        "POLICY_AUTHORITY_INCREASE",
                        f"{layer.kind.value} layer attempted to enable shell authority",
                    )
                shell = shell and layer.process_shell_allowed
            for name in limits:
                proposed = getattr(layer, name)
                if proposed is not None:
                    if proposed > limits[name]:
                        raise PolicyResolutionError(
                            "POLICY_AUTHORITY_INCREASE",
                            f"{layer.kind.value} layer attempted to raise {name}",
                        )
                    limits[name] = min(limits[name], proposed)

        allowed_set = None if allowed is None else set(allowed)
        capability = request.kind.value
        outcome = DecisionOutcome.ALLOW
        reason = "policy_allowed"
        required = tuple(sorted(approvals))
        applicable_approval_classes = {capability}
        if isinstance(request.payload, InstallRequest):
            applicable_approval_classes.add(request.payload.operation)
        decision_approvals = tuple(sorted(approvals & applicable_approval_classes))
        if capability in denied or (allowed_set is not None and capability not in allowed_set):
            outcome = DecisionOutcome.DENY
            reason = "capability_denied"
            decision_approvals = ()
        elif decision_approvals:
            outcome = DecisionOutcome.APPROVAL_REQUIRED
            reason = "approval_required"

        effective = EffectivePolicy(
            id=f"effective-{request.id}",
            run_id=request.run_id,
            created_at=created_at,
            source_layer_digests=tuple(layer.content_digest or "" for layer in policy_layers),
            allowed_capabilities=allowed,
            denied_capabilities=tuple(sorted(denied)),
            writable_paths=paths,
            https_domains=domains,
            network_mode=network,
            process_shell_allowed=shell,
            install_ecosystems=ecosystems,
            max_wall_seconds=float(limits["max_wall_seconds"]),
            max_processes=int(limits["max_processes"]),
            max_worker_turns=int(limits["max_worker_turns"]),
            max_download_bytes=int(limits["max_download_bytes"]),
            max_artifact_bytes=int(limits["max_artifact_bytes"]),
            required_approvals=required,
        )
        decision = PolicyDecision(
            id=decision_id,
            run_id=request.run_id,
            created_at=created_at,
            request_digest=request.content_digest or "",
            effective_policy_digest=effective.content_digest or "",
            outcome=outcome,
            reason_code=reason,
            limits=freeze_json({name: value for name, value in limits.items()}),
            required_approval_classes=decision_approvals,
        )
        return PolicyResolution(effective_policy=effective, decision=decision)
