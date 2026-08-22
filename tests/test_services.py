from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ai_employee.context import ContextCompiler, ROLE_DEFAULTS
from ai_employee.domain import (
    CompletionCriterion, ContextRole, ExecutionStrategy, Finding, MergeDecisionState,
    Reference, ReviewAssessment, RoutingMode, Severity,
    VerificationEvidence, VerificationRequirement,
)
from ai_employee.evidence import aggregate_coverage, assess_completion, build_evidence_pack, decide_merge
from ai_employee.project import discover_project_profile
from ai_employee.routing import record_outcome, select_strategy
from ai_employee.serialization import canonical_digest
from ai_employee.storage import SQLiteStore


class ServiceTests(unittest.TestCase):
    def test_context_defaults_are_role_scoped_and_pull_on_demand(self) -> None:
        source = {"doc": {"secret": "only by reference"}}
        reference = Reference(kind="document", target_id="doc", digest=canonical_digest(source["doc"]))
        package = ContextCompiler().compile(
            package_id="context-test", run_id="run-test", role=ContextRole.WORKER,
            sources=source, references=(reference,),
        )
        self.assertEqual(package.authoritative_refs, (reference,))
        self.assertEqual(dict(package.inline_items), {})
        self.assertFalse(ROLE_DEFAULTS[ContextRole.WORKER].include_history)
        self.assertEqual(ContextCompiler.resolve(reference, source), source["doc"])

    def test_inference_is_provisional_and_never_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "pyproject.toml").write_text("[project]\nname='x'\n")
            profile = discover_project_profile(directory)
            self.assertTrue(profile.rules[0].provisional)
            self.assertFalse(Path(directory, ".fleet").exists())

    def test_evidence_gates_merge_and_completion(self) -> None:
        requirement = VerificationRequirement(
            id="requirement", description="must pass", accepted_evidence_kinds=("test",),
        )
        evidence = VerificationEvidence(
            id="evidence", requirement_ids=(requirement.id,), kind="test", passed=True,
            summary="passed", produced_at=datetime.now(timezone.utc), producer="verifier",
        )
        coverage = aggregate_coverage((requirement,), (evidence,))
        self.assertTrue(coverage.complete)
        assessment = assess_completion(
            criteria=(CompletionCriterion(
                id="criterion", description="verified",
                verification_requirement_ids=(requirement.id,),
            ),),
            coverage=coverage, artifacts=(), mandatory_gates_passed=True,
        )
        self.assertTrue(assessment.complete)
        pack = build_evidence_pack(
            pack_id="pack", run_id="run", contract_ids=(), requirements=(requirement,), evidence=(evidence,),
        )
        decision = decide_merge(
            pack, mandatory_approval_required=True, mandatory_approval_satisfied=False,
        )
        self.assertEqual(decision.state, MergeDecisionState.HUMAN_REVIEW_REQUIRED)

    def test_blocking_review_requires_changes(self) -> None:
        finding = Finding(
            id="finding", code="broken", severity=Severity.HIGH, summary="broken", blocking=True,
        )
        review = ReviewAssessment(
            id="review", reviewer="reviewer", approved=False, blocking_findings=(finding,),
            summary="changes required", assessed_at=datetime.now(timezone.utc),
        )
        pack = build_evidence_pack(
            pack_id="pack-review", run_id="run", contract_ids=(), requirements=(), evidence=(),
            reviews=(review,),
        )
        decision = decide_merge(pack, mandatory_approval_required=False, mandatory_approval_satisfied=False)
        self.assertEqual(decision.state, MergeDecisionState.CHANGES_REQUIRED)

    def test_adaptive_routing_uses_fallback_then_explainable_history(self) -> None:
        strategies = tuple(
            ExecutionStrategy(
                id=name, routing_mode=RoutingMode.ADAPTIVE, backend="local", model=name,
            )
            for name in ("a", "b")
        )
        selected = select_strategy(strategies, mode=RoutingMode.ADAPTIVE)
        self.assertEqual(selected.id, "a")
        self.assertIn("insufficient history", selected.routing_reasons[0])
        histories = []
        for strategy, successes in (("a", 1), ("b", 3)):
            value = None
            for index in range(3):
                value = record_outcome(
                    value, strategy_id=strategy, succeeded=index < successes,
                    duration_seconds=1.0, cost=0.0,
                )
            histories.append(value)
        selected = select_strategy(strategies, mode=RoutingMode.ADAPTIVE, performances=histories)
        self.assertEqual(selected.id, "b")
        self.assertIn("success_rate=1.000", selected.routing_reasons)
        with SQLiteStore(":memory:") as store:
            store.save_performance("project", histories[1])
            self.assertEqual(store.performance("project")[0].sample_count, 3)


if __name__ == "__main__":
    unittest.main()
