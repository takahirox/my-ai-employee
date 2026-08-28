from __future__ import annotations

import unittest

from ai_employee.domain import (
    SemanticAmbiguity,
    SemanticReasoningClass,
    SemanticScope,
    SemanticTaskProfile,
    SemanticTaskType,
)
from ai_employee.routing import (
    RoutingError,
    assess_task,
    merge_semantic_profile,
    profile_compatibility_bands,
)
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

    def test_uses_neutral_bands_and_persists_context_count_without_routing_on_it(self) -> None:
        assessment = assess_task(
            "x" * 1001 + "\nsecond",
            run_id="run.score",
            risk=7,
            required_capabilities=("repository_read", "python_edit"),
        )

        self.assertEqual((assessment.complexity, assessment.scale), (1, 1))
        self.assertEqual(assessment.context_character_count, 1008)
        self.assertEqual(assessment.risk, 7)
        self.assertTrue(all(item.risk == 7 for item in assessment.decomposition))
        self.assertEqual(
            assessment.required_capabilities,
            ("repository_read", "python_edit"),
        )
        profile = SemanticTaskProfile(
            task_type=SemanticTaskType.MECHANICAL,
            reasoning_class=SemanticReasoningClass.MECHANICAL,
            scope=SemanticScope.BOUNDED,
            ambiguity=SemanticAmbiguity.LOW,
            reasons=("one explicit operation",),
        )
        short = merge_semantic_profile(assess_task("Rename x", run_id="run.short"), profile)
        long = merge_semantic_profile(assessment, profile)
        self.assertEqual((short.complexity, short.scale), (1, 1))
        self.assertEqual((long.complexity, long.scale), (1, 1))
        self.assertNotEqual(short.context_character_count, long.context_character_count)

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

    def test_profile_merge_preserves_risk_and_capabilities(self) -> None:
        deterministic = assess_task(
            "Short but architecture-sensitive migration",
            run_id="run.semantic",
            risk=6,
            required_capabilities=("edit_intent", "process"),
        )
        profile = SemanticTaskProfile(
            task_type=SemanticTaskType.OPEN_ENDED_STRATEGY,
            reasoning_class=SemanticReasoningClass.DEEP,
            scope=SemanticScope.BROAD,
            ambiguity=SemanticAmbiguity.HIGH,
            reasons=("cross-cutting migration with compatibility constraints",),
        )
        merged = merge_semantic_profile(deterministic, profile)
        self.assertEqual((merged.complexity, merged.scale, merged.risk), (9, 8, 6))
        self.assertEqual(merged.required_capabilities, ("edit_intent", "process"))
        self.assertEqual(merged.semantic_profile, profile)

    def test_every_profile_enum_has_an_exhaustive_deterministic_mapping(self) -> None:
        task_floors = {
            SemanticTaskType.MECHANICAL: 1,
            SemanticTaskType.RETRIEVAL: 2,
            SemanticTaskType.DIAGNOSIS: 4,
            SemanticTaskType.IMPLEMENTATION: 3,
            SemanticTaskType.ARCHITECTURE: 7,
            SemanticTaskType.RESEARCH: 6,
            SemanticTaskType.PLANNING: 4,
            SemanticTaskType.OPEN_ENDED_STRATEGY: 9,
        }
        reasoning_floors = {
            SemanticReasoningClass.MECHANICAL: 1,
            SemanticReasoningClass.SIMPLE: 2,
            SemanticReasoningClass.MODERATE: 4,
            SemanticReasoningClass.DEEP: 7,
            SemanticReasoningClass.OPEN_ENDED: 9,
        }
        ambiguity_floors = {
            SemanticAmbiguity.LOW: 1,
            SemanticAmbiguity.MEDIUM: 4,
            SemanticAmbiguity.HIGH: 7,
        }
        scopes = {
            SemanticScope.BOUNDED: 1,
            SemanticScope.LOCAL: 2,
            SemanticScope.MULTI_COMPONENT: 5,
            SemanticScope.BROAD: 8,
        }

        def profile(**updates: object) -> SemanticTaskProfile:
            values: dict[str, object] = {
                "task_type": SemanticTaskType.MECHANICAL,
                "reasoning_class": SemanticReasoningClass.MECHANICAL,
                "scope": SemanticScope.BOUNDED,
                "ambiguity": SemanticAmbiguity.LOW,
                "reasons": ("mapping fixture",),
            }
            values.update(updates)
            return SemanticTaskProfile.model_validate(values, strict=True)

        for value, expected in task_floors.items():
            self.assertEqual(profile_compatibility_bands(profile(task_type=value)), (expected, 1))
        for value, expected in reasoning_floors.items():
            self.assertEqual(
                profile_compatibility_bands(profile(reasoning_class=value)), (expected, 1)
            )
        for value, expected in ambiguity_floors.items():
            self.assertEqual(profile_compatibility_bands(profile(ambiguity=value)), (expected, 1))
        for value, expected in scopes.items():
            candidate = profile(scope=value)
            self.assertEqual(profile_compatibility_bands(candidate), (1, expected))
            self.assertEqual(profile_compatibility_bands(candidate), (1, expected))


if __name__ == "__main__":
    unittest.main()
