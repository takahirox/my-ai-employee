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


def authority_supported(authority: Authority) -> None:
    # Current product has coarse HTTPS grants, no credential/service read adapter
    # or operation-level approval enforcement. Do not promise those capabilities.
    if authority.credentials or authority.operation_approval or authority.duplicate_prevention:
        raise Stopped("REQUIRED_AUTHORITY_BOUNDARY_UNAVAILABLE")
    if authority.network_hosts and not authority.external_writes:
        raise Stopped("READ_ONLY_NETWORK_BOUNDARY_UNAVAILABLE")


def readiness(task: Task, config: RunConfig) -> dict[str, object]:
    if not task.authority.within(config.authority_ceiling):
        raise Stopped("AUTHORITY_EXCEEDS_POLICY")
    if (
        task.authority.external_writes
        and config.security == "strict"
        and not (task.authority.operation_approval and task.authority.duplicate_prevention)
    ):
        raise Stopped("STRICT_OPERATION_BOUNDARY_REQUIRED")
    authority_supported(task.authority)
    # Generic model prose cannot prove a remote mutation. A protected operator
    # check must provide the external evidence until a service reader exists.
    checks = {key for criterion in task.criteria for key in criterion.checks}
    external_checks = {
        check.id for check in config.checks if check.evidence_kind == "external_effect"
    }
    if task.authority.external_writes and not checks & external_checks:
        raise Stopped("EXTERNAL_VERIFICATION_PATH_UNAVAILABLE")
    if any(
        criterion.outcome == "external_effect" and not set(criterion.checks) & external_checks
        for criterion in task.criteria
    ):
        raise Stopped("EXTERNAL_VERIFICATION_PATH_UNAVAILABLE")
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
