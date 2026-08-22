"""Runtime-owned safety floor and deterministic policy composition."""

from __future__ import annotations

import math

from pydantic import Field, field_validator

from .base import Identifier, SchemaModel
from .enums import FailureKind
from .models import ExecutionPolicy, Failure


class SafetyPolicyFloor(SchemaModel):
    """Hard limits that project and candidate input cannot override."""

    floor_version: str = Field(default="1", pattern=r"^1$")
    forbidden_capabilities: tuple[Identifier, ...] = (
        "credentials.read",
        "network.unrestricted",
        "process.unrestricted",
        "runtime.self_modify",
    )
    mandatory_approvals: tuple[Identifier, ...] = (
        "publish",
        "deploy",
        "merge",
        "destructive_write",
    )
    max_nodes: int = Field(default=100, ge=1)
    max_attempts: int = Field(default=10, ge=1)
    max_wall_seconds: float = Field(default=3600.0, gt=0)
    network_enabled: bool = False
    unrestricted_process_enabled: bool = False

    @field_validator("max_wall_seconds")
    @classmethod
    def _finite_wall_time(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("safety floor durations must be finite")
        return value


DEFAULT_SAFETY_FLOOR = SafetyPolicyFloor()


class PolicyCompositionError(ValueError):
    """Rejected policy proposal carrying a stable policy failure."""

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def _reject(code: str, message: str, details: object) -> PolicyCompositionError:
    return PolicyCompositionError(
        Failure(
            id="policy_failure",
            kind=FailureKind.POLICY,
            code=code,
            message=message,
            retryable=False,
            details=details,
        )
    )


def compose_execution_policy(
    *proposals: ExecutionPolicy,
    floor: SafetyPolicyFloor = DEFAULT_SAFETY_FLOOR,
) -> ExecutionPolicy:
    """Compose restrictions while proving that no proposal weakens the floor.

    Policy inputs may add denials and approvals or lower resource limits. Enabling
    a hard-disabled authority or raising a hard cap never changes the
    runtime-owned floor.
    """

    missing_denials = set(DEFAULT_SAFETY_FLOOR.forbidden_capabilities) - set(
        floor.forbidden_capabilities
    )
    missing_approvals = set(DEFAULT_SAFETY_FLOOR.mandatory_approvals) - set(
        floor.mandatory_approvals
    )
    raised_floor_caps = {
        name: {"requested": requested, "hard_cap": hard_cap}
        for name, requested, hard_cap in (
            ("max_nodes", floor.max_nodes, DEFAULT_SAFETY_FLOOR.max_nodes),
            ("max_attempts", floor.max_attempts, DEFAULT_SAFETY_FLOOR.max_attempts),
            (
                "max_wall_seconds",
                floor.max_wall_seconds,
                DEFAULT_SAFETY_FLOOR.max_wall_seconds,
            ),
        )
        if requested > hard_cap
    }
    enabled_authorities = []
    if floor.network_enabled and not DEFAULT_SAFETY_FLOOR.network_enabled:
        enabled_authorities.append("network")
    if (
        floor.unrestricted_process_enabled
        and not DEFAULT_SAFETY_FLOOR.unrestricted_process_enabled
    ):
        enabled_authorities.append("unrestricted_process")
    if missing_denials or missing_approvals or raised_floor_caps or enabled_authorities:
        raise _reject(
            "runtime_floor_violation",
            "a supplied safety floor cannot weaken the built-in runtime floor",
            {
                "missing_denials": sorted(missing_denials),
                "missing_approvals": sorted(missing_approvals),
                "raised_caps": raised_floor_caps,
                "enabled_authorities": enabled_authorities,
            },
        )

    denied = set(floor.forbidden_capabilities)
    approvals = set(floor.mandatory_approvals)
    max_nodes = floor.max_nodes
    max_attempts = floor.max_attempts
    max_wall_seconds = floor.max_wall_seconds

    for proposal in proposals:
        if proposal.network_enabled and not floor.network_enabled:
            raise _reject(
                "network_floor_violation",
                "project or candidate policy cannot enable network authority",
                {"proposal_version": proposal.policy_version},
            )
        if proposal.unrestricted_process_enabled and not floor.unrestricted_process_enabled:
            raise _reject(
                "process_floor_violation",
                "project or candidate policy cannot enable unrestricted process authority",
                {"proposal_version": proposal.policy_version},
            )
        cap_violations = {
            "max_nodes": (proposal.max_nodes, floor.max_nodes),
            "max_attempts": (proposal.max_attempts, floor.max_attempts),
            "max_wall_seconds": (proposal.max_wall_seconds, floor.max_wall_seconds),
        }
        raised = {
            name: {"requested": requested, "hard_cap": hard_cap}
            for name, (requested, hard_cap) in cap_violations.items()
            if requested > hard_cap
        }
        if raised:
            raise _reject(
                "hard_cap_violation",
                "project or candidate policy cannot raise runtime hard caps",
                raised,
            )
        denied.update(proposal.denied_capabilities)
        approvals.update(proposal.required_approvals)
        max_nodes = min(max_nodes, proposal.max_nodes)
        max_attempts = min(max_attempts, proposal.max_attempts)
        max_wall_seconds = min(max_wall_seconds, proposal.max_wall_seconds)

    return ExecutionPolicy(
        policy_version=floor.floor_version,
        denied_capabilities=tuple(sorted(denied)),
        required_approvals=tuple(sorted(approvals)),
        max_nodes=max_nodes,
        max_attempts=max_attempts,
        max_wall_seconds=max_wall_seconds,
        network_enabled=floor.network_enabled,
        unrestricted_process_enabled=floor.unrestricted_process_enabled,
    )
