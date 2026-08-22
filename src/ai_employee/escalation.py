"""Deterministic failure escalation policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .domain import Budget, Failure, FailureKind


class EscalationAction(StrEnum):
    RETRY = "retry"
    REPLAN = "replan"
    FAIL = "fail"
    EXHAUST = "exhaust"
    BLOCK = "block"
    CANCEL = "cancel"


@dataclass(frozen=True)
class EscalationDecision:
    action: EscalationAction
    reason: str
    rule_version: str = "escalation-v1"


def decide_escalation(
    failure: Failure,
    *,
    attempt: int,
    replan_count: int,
    budget: Budget,
    node_retry_limit: int,
) -> EscalationDecision:
    """Choose one stable action from structured failure facts and hard budgets."""

    if failure.kind is FailureKind.CANCELLATION:
        return EscalationDecision(EscalationAction.CANCEL, "cancellation is terminal")
    if failure.kind is FailureKind.EXTERNAL_BLOCKER:
        return EscalationDecision(EscalationAction.BLOCK, "external coordination is required")
    if failure.kind is FailureKind.RESOURCE_EXHAUSTION:
        return EscalationDecision(EscalationAction.EXHAUST, "a hard resource budget was exhausted")
    retry_cap = min(node_retry_limit, budget.max_retries)
    if failure.retryable and attempt < retry_cap:
        return EscalationDecision(
            EscalationAction.RETRY, "retryable failure remains within retry budget"
        )
    if failure.kind in {FailureKind.GRAPH, FailureKind.VALIDATION}:
        if replan_count < budget.max_replans:
            return EscalationDecision(
                EscalationAction.REPLAN, "graph failure remains within replan budget"
            )
        return EscalationDecision(EscalationAction.EXHAUST, "replan budget was exhausted")
    return EscalationDecision(EscalationAction.FAIL, "failure is not safely retryable")
