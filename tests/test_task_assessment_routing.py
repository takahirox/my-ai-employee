from __future__ import annotations

import unittest

from ai_employee.domain import SemanticTaskAssessment
from ai_employee.routing import RoutingError, assess_task, merge_semantic_assessment
from ai_employee.serialization import canonical_digest


class TaskAssessmentRoutingTests(unittest.TestCase):
    def test_normalizes_goal_and_builds_canonical_digest_ids(self) -> None:
        arguments = {
            "run_id": "run.assess",
            "risk": 2,
            "required_capabilities": ("python_edit",),
        }
        normalized = assess_task("\uff26ix tests", **arguments)
        repeated = assess_task("Fix tests", **arguments)
        expected_identity = {
            "run_id": "run.assess",
            "goal_digest": normalized.goal_digest,
            "risk": 2,
            "required_capabilities": ("python_edit",),
        }

        self.assertEqual(normalized, repeated)
        self.assertEqual(normalized.goal_digest, canonical_digest("Fix tests"))
        self.assertEqual(
            normalized.id,
            f"assessment.{canonical_digest(expected_identity)}",
        )
        self.assertEqual(
            normalized.decomposition[0].id,
            (f"assessment-item.{canonical_digest((normalized.goal_digest, 1, 'Fix tests'))}"),
        )

    def test_structurally_splits_unicode_line_breaks_and_semicolons(self) -> None:
        assessment = assess_task(
            "Inspect\r\nImplement;\u2028Test\u2029Document\u0085Report",
            run_id="run.split",
        )

        self.assertEqual(
            tuple(item.title for item in assessment.decomposition),
            ("Inspect", "Implement", "Test", "Document", "Report"),
        )
        self.assertTrue(
            all("assessment only" in item.reasons[-1] for item in assessment.decomposition)
        )

    def test_decomposition_is_bounded_to_twenty_items(self) -> None:
        assessment = assess_task(
            ";".join(f"item {index}" for index in range(25)),
            run_id="run.bound",
        )

        self.assertEqual(len(assessment.decomposition), 20)
        self.assertEqual(assessment.decomposition[-1].title, "item 19")

    def test_scores_transparently_and_preserves_risk(self) -> None:
        assessment = assess_task(
            "x" * 1001 + "\nsecond",
            run_id="run.score",
            risk=7,
            required_capabilities=("repository_read", "python_edit"),
        )

        self.assertEqual((assessment.complexity, assessment.scale), (4, 2))
        self.assertEqual(assessment.risk, 7)
        self.assertTrue(all(item.risk == 7 for item in assessment.decomposition))
        self.assertEqual(
            assessment.required_capabilities,
            ("repository_read", "python_edit"),
        )
        self.assertEqual(
            assessment.reasons,
            (
                ("complexity=4 from goal_length=1008, item_count=2, and capability_count=2"),
                "scale=2 from structural_item_count=2",
                "risk=7 preserved from caller input",
            ),
        )

    def test_rejects_invalid_bounds(self) -> None:
        cases = (
            ("", 0, ()),
            ("x" * 10_001, 0, ()),
            ("goal", -1, ()),
            ("goal", 11, ()),
            (
                "goal",
                0,
                tuple(f"capability.{index}" for index in range(51)),
            ),
        )
        for goal, risk, capabilities in cases:
            with (
                self.subTest(goal_length=len(goal), risk=risk, capabilities=len(capabilities)),
                self.assertRaises(RoutingError),
            ):
                assess_task(
                    goal,
                    run_id="run.invalid",
                    risk=risk,
                    required_capabilities=capabilities,
                )

    def test_semantic_assessment_can_only_raise_deterministic_floors(self) -> None:
        deterministic = assess_task(
            "Short but architecture-sensitive migration",
            run_id="run.semantic",
            risk=6,
            required_capabilities=("edit_intent", "process"),
        )
        semantic = SemanticTaskAssessment(
            complexity=9,
            scale=7,
            required_capabilities=("install",),
            reasons=("cross-cutting migration with compatibility constraints",),
        )

        merged = merge_semantic_assessment(
            deterministic,
            semantic,
            available_capabilities=("edit_intent", "process", "install"),
        )

        self.assertEqual((merged.complexity, merged.scale, merged.risk), (9, 7, 6))
        self.assertEqual(merged.required_capabilities, ("edit_intent", "process", "install"))
        self.assertIn("semantic assessment:", merged.reasons[-1])

        lower = SemanticTaskAssessment(
            complexity=1,
            scale=1,
            reasons=("appears small",),
        )
        preserved = merge_semantic_assessment(
            merged,
            lower,
            available_capabilities=("edit_intent", "process", "install"),
        )
        self.assertEqual((preserved.complexity, preserved.scale), (9, 7))

    def test_semantic_assessment_rejects_unknown_capabilities(self) -> None:
        deterministic = assess_task("Goal", run_id="run.semantic-invalid")
        semantic = SemanticTaskAssessment(
            complexity=2,
            scale=1,
            required_capabilities=("unavailable_tool",),
            reasons=("requires an unavailable tool",),
        )

        with self.assertRaisesRegex(RoutingError, "unsupported capabilities"):
            merge_semantic_assessment(
                deterministic,
                semantic,
                available_capabilities=("edit_intent", "process"),
            )


if __name__ == "__main__":
    unittest.main()
