from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from ai_employee.domain.base import Identifier
from ai_employee.domain.v2 import (
    ApprovalRecord,
    ApprovalRequest,
    DecisionOutcome,
    PolicyDecision,
)
from ai_employee.storage import SQLiteStore

from ._common import identifier, now

if TYPE_CHECKING:
    from ai_employee.promotion_approval import PromotionPolicyDecision


class _ApprovalConsumption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier
    run_id: Identifier
    request_digest: str


class DigestApprovalService:
    """Persistent, single-use approvals bound to exact request and policy digests."""

    _KIND = "approval_v2"
    _CONSUMED_KIND = "approval_v2_consumed"

    def __init__(
        self,
        store: SQLiteStore,
        *,
        operator_label: str,
        clock: Callable[[], datetime] = now,
    ) -> None:
        self.store = store
        self.operator_label = operator_label
        self.clock = clock

    def request(self, request: ApprovalRequest, decision: PolicyDecision) -> ApprovalRecord:
        current = self.clock()
        if decision.outcome is not DecisionOutcome.APPROVAL_REQUIRED:
            raise ValueError("only approval_required decisions may create approvals")
        if request.request_digest != decision.request_digest:
            raise ValueError("approval request digest does not match policy decision")
        if request.policy_digest != decision.effective_policy_digest:
            raise ValueError("approval policy digest does not match policy decision")
        if set(request.approval_classes) != set(decision.required_approval_classes):
            raise ValueError("approval classes must exactly match the policy decision")
        if request.expires_at <= current:
            raise ValueError("approval request is already expired")
        record = ApprovalRecord(
            id=identifier("approval"),
            run_id=request.run_id,
            created_at=current,
            request_digest=request.request_digest,
            policy_digest=request.policy_digest,
            scope=(request.request_digest,),
            decision="pending",
            operator_label=self.operator_label,
            expires_at=request.expires_at,
        )
        self.store.put(self._KIND, record, run_id=record.run_id)
        return record

    def request_policy_auto(
        self,
        request: ApprovalRequest,
        decision: PolicyDecision,
        authority: PromotionPolicyDecision,
    ) -> ApprovalRecord:
        """Persist or recover one exact policy-issued approval; never bypass the record."""

        self._validate_request(request, decision)
        current = self.clock()
        authority_digest = authority.content_digest
        if (
            authority_digest is None
            or authority.run_id != request.run_id
            or authority.decision != "policy_auto_approved"
            or authority.candidate_digest != request.request_digest
            or authority.effective_policy_digest != request.policy_digest
            or authority.created_at > current
            or request.expires_at <= current
        ):
            raise ValueError("promotion policy authority is stale or mismatched")
        expected: dict[str, object] = {
            "request_digest": request.request_digest,
            "policy_digest": request.policy_digest,
            "scope": (request.request_digest,),
            "decision": "approved",
            "authorization_kind": "policy_auto",
            "authorization_digest": authority_digest,
            "rule_id": authority.rule_id,
            "reason_code": authority.reason_code,
            "accepted_graph_revision_digest": authority.accepted_graph_revision_digest,
            "harness_digest": authority.harness_digest,
            "operator_config_digest": authority.operator_config_digest,
            "parent_evaluation_digest": authority.parent_evaluation_digest,
            "verification_evidence_digests": authority.verification_evidence_digests,
            "evaluation_evidence_digests": authority.evaluation_ledger_digests,
            "semantic_evidence_digests": authority.semantic_evidence_digests,
        }
        existing = tuple(
            item
            for item in self.store.list_records(self._KIND, ApprovalRecord, run_id=request.run_id)
            if item.authorization_digest == authority_digest
        )
        if len(existing) > 1:
            raise ValueError("promotion policy approval is ambiguous")
        if existing:
            record = existing[0]
            if any(getattr(record, name) != value for name, value in expected.items()):
                raise ValueError("promotion policy approval recovery is mismatched")
            return record
        record = ApprovalRecord(
            id=identifier("approval"),
            run_id=request.run_id,
            created_at=current,
            request_digest=request.request_digest,
            policy_digest=request.policy_digest,
            scope=(request.request_digest,),
            decision="approved",
            operator_label=f"policy:{authority.rule_id}",
            expires_at=request.expires_at,
            decided_at=current,
            authorization_kind="policy_auto",
            authorization_digest=authority_digest,
            rule_id=authority.rule_id,
            reason_code=authority.reason_code,
            accepted_graph_revision_digest=authority.accepted_graph_revision_digest,
            harness_digest=authority.harness_digest,
            operator_config_digest=authority.operator_config_digest,
            parent_evaluation_digest=authority.parent_evaluation_digest,
            verification_evidence_digests=authority.verification_evidence_digests,
            evaluation_evidence_digests=authority.evaluation_ledger_digests,
            semantic_evidence_digests=authority.semantic_evidence_digests,
        )
        self.store.put(self._KIND, record, run_id=record.run_id)
        return record

    def decide(
        self,
        approval_id: Identifier,
        request_digest: str,
        decision: Literal["approved", "denied"],
    ) -> ApprovalRecord:
        existing = self.store.get(self._KIND, approval_id, ApprovalRecord)
        current = self.clock()
        if existing.request_digest != request_digest:
            raise ValueError("approval request digest mismatch")
        if existing.decision != "pending":
            raise ValueError("approval has already been decided")
        outcome: Literal["approved", "denied", "expired"] = decision
        if current >= existing.expires_at:
            outcome = "expired"
        updated = ApprovalRecord.model_validate(
            {
                **existing.model_dump(mode="python"),
                "decision": outcome,
                "decided_at": current,
                "content_digest": None,
            },
            strict=True,
        )
        if not self.store.put_once(self._KIND, updated, run_id=updated.run_id, revision=2):
            raise ValueError("approval has already been decided")
        if outcome == "expired":
            raise ValueError("approval has expired")
        return updated

    def authorize(self, decision: PolicyDecision, approval: ApprovalRecord) -> bool:
        """Convert approval-required to allow, but never a policy denial."""
        return bool(
            decision.outcome is DecisionOutcome.APPROVAL_REQUIRED
            and approval.decision == "approved"
            and approval.request_digest == decision.request_digest
            and approval.policy_digest == decision.effective_policy_digest
            and self.clock() < approval.expires_at
            and not self._consumed(approval.id)
        )

    def apply(self, decision: PolicyDecision, approval: ApprovalRecord) -> PolicyDecision:
        """Return a new allow decision only for a valid approval-required decision."""
        if decision.outcome is DecisionOutcome.DENY:
            raise ValueError("approval cannot override a policy denial")
        if decision.outcome is not DecisionOutcome.APPROVAL_REQUIRED:
            raise ValueError("decision does not require approval")
        if not self.authorize(decision, approval):
            raise ValueError("approval is stale, denied, expired, or digest-mismatched")
        if not self.store.put_once(
            self._CONSUMED_KIND,
            _ApprovalConsumption(
                id=approval.id,
                run_id=approval.run_id,
                request_digest=approval.request_digest,
            ),
            run_id=approval.run_id,
        ):
            raise ValueError("approval is stale, denied, expired, or digest-mismatched")
        payload = decision.model_dump()
        payload.update(
            {
                "id": identifier("approved-decision"),
                "created_at": self.clock(),
                "outcome": DecisionOutcome.ALLOW,
                "reason_code": "approved",
                "required_approval_classes": (),
                "content_digest": None,
            }
        )
        return PolicyDecision.model_validate(payload, strict=True)

    def _consumed(self, approval_id: Identifier) -> bool:
        try:
            self.store.get(self._CONSUMED_KIND, approval_id, _ApprovalConsumption)
        except KeyError:
            return False
        return True

    def _validate_request(self, request: ApprovalRequest, decision: PolicyDecision) -> None:
        if decision.outcome is not DecisionOutcome.APPROVAL_REQUIRED:
            raise ValueError("only approval_required decisions may create approvals")
        if request.request_digest != decision.request_digest:
            raise ValueError("approval request digest does not match policy decision")
        if request.policy_digest != decision.effective_policy_digest:
            raise ValueError("approval policy digest does not match policy decision")
        if set(request.approval_classes) != set(decision.required_approval_classes):
            raise ValueError("approval classes must exactly match the policy decision")
