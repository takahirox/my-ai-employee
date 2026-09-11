"""Product capability facts; environment readiness and Run permission stay separate."""

from __future__ import annotations

from .history import Stopped
from .models import Authority, RunConfig, StagePolicy, Task
from .product_capabilities import SUPPORTED_BACKENDS as SUPPORTED_BACKENDS
from .stage_contracts import digest


def policies(config: RunConfig) -> tuple[StagePolicy, ...]:
    return (
        config.clarification,
        config.planning,
        config.worker,
        config.verification,
        config.recovery,
        *config.worker_options,
        *config.worker_escalations,
        *((config.selection,) if config.selection else ()),
    )


def validate_policy(policy: StagePolicy) -> None:
    if policy.backend not in SUPPORTED_BACKENDS or (
        policy.review != "never"
        and (policy.reviewer_backend or policy.backend) not in SUPPORTED_BACKENDS
    ):
        raise Stopped("UNSUPPORTED_BACKEND")


# These product facts constrain generation and execution, including native preflight.
UNSUPPORTED_AUTHORITY = ("credentials", "operation_approval", "duplicate_prevention")
AUTHORITY_RULES = {
    "EXTERNAL_GOAL_WEAKENED": {
        "path": "tasks.criteria",
        "rule": "A Plan must preserve each Goal external_effect evidence check in an "
        "external_effect task criterion; a local artifact cannot replace that outcome.",
    },
    "AUTHORITY_EXCEEDS_POLICY": {
        "path": "authority",
        "rule": "Hosts and credentials must fit the Run ceiling; "
        "external writes require permission "
        "and any operation controls required by that ceiling. Proposals never grant permission.",
    },
    "STRICT_OPERATION_BOUNDARY_REQUIRED": {
        "path": "authority.operation_approval/duplicate_prevention",
        "rule": "Strict external writes require both operation approval and duplicate prevention.",
    },
    "REQUIRED_AUTHORITY_BOUNDARY_UNAVAILABLE": {
        "path": "authority",
        "rule": "The product cannot enforce the listed unsupported authority fields.",
    },
    "READ_ONLY_NETWORK_BOUNDARY_UNAVAILABLE": {
        "path": "authority.network_hosts",
        "rule": "Network hosts require external_writes: "
        "the product cannot enforce read-only HTTPS.",
    },
    "EXTERNAL_VERIFICATION_PATH_UNAVAILABLE": {
        "path": "criteria.checks",
        "rule": "External writes need an operator external_effect check; each external_effect "
        "criterion must reference such a check. Model prose cannot prove remote completion.",
    },
}


def authority_projection(config: RunConfig) -> dict[str, object]:
    properties: dict[str, object] = {}
    for name, field in Authority.model_fields.items():
        # Keep requirements expressible, including infeasible ones. A false-only
        # schema would force the producer to erase a genuinely required control.
        properties[name] = {
            "description": f"{field.description} "
            + (
                "Unsupported by this product; a nonempty/true request is rejected. "
                if name in UNSUPPORTED_AUTHORITY
                else ""
            )
            + "Propose only what the unchanged Goal needs. Run ceiling value: "
            + str(getattr(config.authority_ceiling, name))
            + ". See the shared authority rules for conditional policy constraints."
        }
    return {
        "properties": properties,
        "unsupported_fields": UNSUPPORTED_AUTHORITY,
        "authority_ceiling": config.authority_ceiling.model_dump(mode="json"),
        "security": config.security,
        "rules": AUTHORITY_RULES,
        "external_evidence_checks": [
            c.id for c in config.checks if c.evidence_kind == "external_effect"
        ],
        "environment": "Actual environment availability is checked by invocation preflight; "
        "this contract does not assert a successful probe or authentication.",
        "repair": "Repair proposals only within the unchanged Goal and policy. Never remove "
        "necessary controls or replace required external effects with local artifacts to fit "
        "these constraints. If a necessary capability is unavailable, do not claim feasibility; "
        "retain the required authority/outcome so deterministic validation stops execution.",
    }


def authority_policy_failure(authority: Authority, config: RunConfig) -> str | None:
    if not authority.within(config.authority_ceiling):
        return "AUTHORITY_EXCEEDS_POLICY"
    if (
        authority.external_writes
        and config.security == "strict"
        and not (authority.operation_approval and authority.duplicate_prevention)
    ):
        return "STRICT_OPERATION_BOUNDARY_REQUIRED"
    return None


def authority_support_failure(authority: Authority) -> str | None:
    if any(getattr(authority, field) for field in UNSUPPORTED_AUTHORITY):
        return "REQUIRED_AUTHORITY_BOUNDARY_UNAVAILABLE"
    if authority.network_hosts and not authority.external_writes:
        return "READ_ONLY_NETWORK_BOUNDARY_UNAVAILABLE"
    return None


def authority_supported(authority: Authority) -> None:
    failure = authority_support_failure(authority)
    if failure:
        raise Stopped(failure)


def task_violation(task: Task, config: RunConfig) -> dict[str, object] | None:
    reason = authority_policy_failure(task.authority, config) or authority_support_failure(
        task.authority
    )
    external_checks = {c.id for c in config.checks if c.evidence_kind == "external_effect"}
    checks = {key for criterion in task.criteria for key in criterion.checks}
    if reason is None and (
        (task.authority.external_writes and not checks & external_checks)
        or any(
            c.outcome == "external_effect" and not set(c.checks) & external_checks
            for c in task.criteria
        )
    ):
        reason = "EXTERNAL_VERIFICATION_PATH_UNAVAILABLE"
    if reason is None:
        return None
    return {
        "reason": reason,
        **AUTHORITY_RULES[reason],
        "task": task.id,
        "task_digest": task.digest,
        "authority": task.authority.model_dump(mode="json"),
        "unsupported_fields": [f for f in UNSUPPORTED_AUTHORITY if getattr(task.authority, f)],
        "authority_ceiling": config.authority_ceiling.model_dump(mode="json"),
        "security": config.security,
        "external_evidence_checks": sorted(external_checks),
    }


def readiness(task: Task, config: RunConfig) -> dict[str, object]:
    violation = task_violation(task, config)
    if violation:
        raise Stopped(str(violation["reason"]))
    return {
        "task_digest": task.digest,
        "state": "conditionally_plannable" if task.dependencies else "plannable",
        "dependencies": task.dependencies,
        "authority_digest": task.authority.digest,
        "policy_digest": config.digest,
        "criteria": [
            {
                "criterion": criterion.id,
                "outcome": criterion.outcome,
                "checks": criterion.checks,
                "method": "protected_check_and_independent_review"
                if criterion.checks
                else "independent_artifact_review",
                "evidence_digest": digest(task.required_evidence),
                "verification_plan_digest": digest(task.verification_plan),
            }
            for criterion in task.criteria
        ],
        "future_evidence_is_observed": False,
    }
