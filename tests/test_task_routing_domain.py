from __future__ import annotations

import unittest

from pydantic import ValidationError

import ai_employee.domain as domain
from ai_employee.domain import (
    ExecutionStrategy,
    RoutingMode,
    SemanticAmbiguity,
    SemanticReasoningClass,
    SemanticScope,
    SemanticTaskProfile,
    SemanticTaskType,
    TaskAssessment,
    TaskDecompositionItem,
)
from ai_employee.serialization import canonical_json


class TaskRoutingDomainTests(unittest.TestCase):
    def test_execution_strategy_defaults_preserve_legacy_data(self) -> None:
        strategy = ExecutionStrategy.model_validate_json(
            canonical_json(
                {
                    "schema_version": "1",
                    "id": "strategy.legacy",
                    "routing_mode": "policy",
                    "backend": "local",
                    "model": "legacy",
                }
            ),
            strict=True,
        )
        self.assertEqual(strategy.capabilities, ())
        self.assertEqual(
            (
                strategy.min_complexity,
                strategy.max_complexity,
                strategy.min_scale,
                strategy.max_scale,
                strategy.max_risk,
            ),
            (1, 10, 1, 10, 10),
        )

    def test_execution_strategy_validates_suitability_bounds(self) -> None:
        with self.assertRaisesRegex(ValidationError, "minimum complexity"):
            ExecutionStrategy(
                id="strategy.bad-complexity",
                routing_mode=RoutingMode.POLICY,
                backend="local",
                model="model",
                min_complexity=8,
                max_complexity=7,
            )
        with self.assertRaisesRegex(ValidationError, "capabilities must be unique"):
            ExecutionStrategy(
                id="strategy.bad-capabilities",
                routing_mode=RoutingMode.POLICY,
                backend="local",
                model="model",
                capabilities=("coding", "coding"),
            )
        with self.assertRaises(ValidationError):
            ExecutionStrategy(
                id="strategy.bad-risk",
                routing_mode=RoutingMode.POLICY,
                backend="local",
                model="model",
                max_risk=11,
            )

    def test_task_assessment_is_public_and_round_trips(self) -> None:
        item = TaskDecompositionItem(
            id="assessment-item.inspect",
            title="Inspect the relevant domain contracts",
            complexity=3,
            scale=2,
            risk=1,
            required_capabilities=("repository_read",),
            reasons=("The affected surface is limited to domain contracts",),
        )
        profile = SemanticTaskProfile(
            task_type=SemanticTaskType.IMPLEMENTATION,
            reasoning_class=SemanticReasoningClass.MODERATE,
            scope=SemanticScope.LOCAL,
            ambiguity=SemanticAmbiguity.LOW,
            reasons=("a bounded domain implementation",),
        )
        assessment = TaskAssessment(
            id="assessment.route",
            run_id="run.route",
            goal_digest="a" * 64,
            complexity=5,
            scale=4,
            risk=2,
            required_capabilities=("repository_read", "python_edit"),
            decomposition=(item,),
            semantic_profile=profile,
            context_character_count=42,
            reasons=("Two bounded domain changes and focused tests are required",),
        )
        restored = TaskAssessment.model_validate_json(
            canonical_json(assessment),
            strict=True,
        )
        self.assertEqual(restored, assessment)
        self.assertIsInstance(restored.decomposition[0], TaskDecompositionItem)
        self.assertEqual(restored.semantic_profile, profile)
        self.assertLessEqual(
            {
                "SemanticAmbiguity",
                "SemanticReasoningClass",
                "SemanticScope",
                "SemanticTaskProfile",
                "SemanticTaskType",
                "TaskAssessment",
                "TaskDecompositionItem",
            }
            - set(domain.__all__),
            set(),
        )
        legacy = TaskAssessment.model_validate_json(
            '{"id":"assessment.legacy","run_id":"run.route","goal_digest":"'
            + "b" * 64
            + '","complexity":2,"scale":1,"risk":0,"reasons":["legacy"]}',
            strict=True,
        )
        self.assertIsNone(legacy.semantic_profile)
        self.assertIsNone(legacy.context_character_count)

    def test_task_assessment_rejects_invalid_or_unbounded_decomposition(self) -> None:
        with self.assertRaisesRegex(ValidationError, "reasons must be non-blank"):
            TaskDecompositionItem(
                id="assessment-item.blank-reason",
                title="Invalid item",
                complexity=1,
                scale=1,
                risk=0,
                reasons=("",),
            )

        item = TaskDecompositionItem(
            id="assessment-item.valid",
            title="Valid item",
            complexity=1,
            scale=1,
            risk=0,
            reasons=("The item is independently assessable",),
        )
        with self.assertRaisesRegex(ValidationError, "item IDs must be unique"):
            TaskAssessment(
                id="assessment.duplicate-items",
                run_id="run.route",
                goal_digest="b" * 64,
                complexity=2,
                scale=2,
                risk=1,
                decomposition=(item, item),
                reasons=("The proposed decomposition contains duplicate identities",),
            )

        with self.assertRaises(ValidationError):
            TaskAssessment(
                id="assessment.too-many-items",
                run_id="run.route",
                goal_digest="c" * 64,
                complexity=10,
                scale=10,
                risk=5,
                decomposition=tuple(
                    item.model_copy(update={"id": f"assessment-item.{index}"})
                    for index in range(101)
                ),
                reasons=("The decomposition exceeds its persisted domain bound",),
            )


if __name__ == "__main__":
    unittest.main()
