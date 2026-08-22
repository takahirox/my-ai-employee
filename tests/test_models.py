from __future__ import annotations

import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

import ai_employee.domain as domain
from ai_employee.domain import (
    ContractKind,
    EvidenceCoverage,
    Failure,
    FailureKind,
    GateKind,
    GateResult,
    GateStatus,
    OutputContract,
    ProvenanceKind,
    ProvenancedValue,
    ResultEnvelope,
    ResultStatus,
)

from tests.helpers import output_contract


class ModelTests(unittest.TestCase):
    def test_requested_model_inventory_is_public(self) -> None:
        names = {
            "Goal",
            "CompletionCriterion",
            "Constraint",
            "Budget",
            "Plan",
            "Task",
            "Graph",
            "Node",
            "Edge",
            "AcceptedGraphRevision",
            "Run",
            "ExecutionPolicy",
            "ExecutionStrategy",
            "TaskProfile",
            "Event",
            "Artifact",
            "ResultEnvelope",
            "OutputContract",
            "GateResult",
            "Contract",
            "VerificationRequirement",
            "VerificationEvidence",
            "EvidenceCoverage",
            "ReviewAssessment",
            "EvidencePack",
            "MergeDecision",
            "ProjectProfile",
            "ContextPackage",
            "ContextPolicy",
            "ExecutionMetrics",
        }
        self.assertEqual(names - set(domain.__all__), set())

    def test_failure_taxonomy_is_complete(self) -> None:
        self.assertEqual(
            {kind.value for kind in FailureKind},
            {
                "execution",
                "validation",
                "policy",
                "invalid_output",
                "timeout",
                "resource_exhaustion",
                "verification",
                "review",
                "graph",
                "cancellation",
                "external_blocker",
            },
        )
        for kind in FailureKind:
            failure = Failure(
                id=f"failure.{kind.value}",
                kind=kind,
                code="test_failure",
                message="structured failure",
            )
            self.assertEqual(failure.kind, kind)

    def test_result_envelope_validates_output_contract(self) -> None:
        contract = output_contract()
        valid = ResultEnvelope(
            contract_id=contract.id,
            status=ResultStatus.SUCCEEDED,
            value={"answer": 42},
        )
        valid.validate_contract(contract)
        invalid = ResultEnvelope(
            contract_id=contract.id,
            status=ResultStatus.SUCCEEDED,
            value={"claim": "success"},
        )
        with self.assertRaisesRegex(ValueError, "missing required fields"):
            invalid.validate_contract(contract)

    def test_non_object_contract_cannot_declare_fields(self) -> None:
        with self.assertRaises(ValidationError):
            OutputContract(
                id="contract.bad",
                expected_type=ContractKind.STRING,
                required_fields=("answer",),
            )

    def test_gate_timeout_requires_timeout_failure(self) -> None:
        timeout = Failure(
            id="failure.timeout",
            kind=FailureKind.TIMEOUT,
            code="gate_timeout",
            message="gate timed out",
        )
        result = GateResult(
            id="gate.result",
            kind=GateKind.COMMAND_RESULT,
            status=GateStatus.TIMEOUT,
            summary="bounded command result timed out",
            failure=timeout,
        )
        self.assertEqual(result.status, GateStatus.TIMEOUT)
        with self.assertRaises(ValidationError):
            GateResult(
                id="gate.bad",
                kind=GateKind.PREDICATE,
                status=GateStatus.FAILED,
                summary="failed without details",
            )

    def test_evidence_coverage_is_a_partition(self) -> None:
        coverage = EvidenceCoverage(
            requirement_ids=("req.one", "req.two"),
            satisfied_requirement_ids=("req.one",),
            missing_requirement_ids=("req.two",),
            mapping={"req.one": ["evidence.one"]},
            complete=False,
        )
        self.assertFalse(coverage.complete)
        with self.assertRaises(ValidationError):
            EvidenceCoverage(
                requirement_ids=("req.one",),
                satisfied_requirement_ids=("req.one",),
                missing_requirement_ids=("req.one",),
                mapping={},
                complete=True,
            )

    def test_inferred_project_values_remain_provisional(self) -> None:
        with self.assertRaises(ValidationError):
            ProvenancedValue(value="pytest", provenance=ProvenanceKind.INFERRED)
        value = ProvenancedValue(
            value="pytest",
            provenance=ProvenanceKind.INFERRED,
            provisional=True,
        )
        self.assertTrue(value.provisional)

    def test_timestamps_require_an_offset(self) -> None:
        with self.assertRaises(ValidationError):
            domain.Event(
                id="event.one",
                run_id="run.one",
                event_type="node.started",
                timestamp=datetime(2025, 1, 1),
                actor="runtime",
            )
        event = domain.Event(
            id="event.one",
            run_id="run.one",
            event_type="node.started",
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            actor="runtime",
        )
        self.assertIsNotNone(event.timestamp.tzinfo)


if __name__ == "__main__":
    unittest.main()
