from __future__ import annotations

import unittest

from ai_employee.domain import (
    DEFAULT_SAFETY_FLOOR,
    ExecutionPolicy,
    FailureKind,
    PolicyCompositionError,
    SafetyPolicyFloor,
    compose_execution_policy,
)


class PolicyTests(unittest.TestCase):
    def test_floor_is_applied_when_no_project_policy_exists(self) -> None:
        effective = compose_execution_policy()
        self.assertEqual(
            set(effective.denied_capabilities),
            set(DEFAULT_SAFETY_FLOOR.forbidden_capabilities),
        )
        self.assertEqual(
            set(effective.required_approvals),
            set(DEFAULT_SAFETY_FLOOR.mandatory_approvals),
        )
        self.assertFalse(effective.network_enabled)
        self.assertFalse(effective.unrestricted_process_enabled)

    def test_project_policy_can_only_add_restrictions(self) -> None:
        effective = compose_execution_policy(
            ExecutionPolicy(
                denied_capabilities=("filesystem.write",),
                required_approvals=("artifact.export",),
                max_nodes=20,
                max_attempts=3,
                max_wall_seconds=60.0,
            )
        )
        self.assertIn("filesystem.write", effective.denied_capabilities)
        self.assertIn("credentials.read", effective.denied_capabilities)
        self.assertIn("publish", effective.required_approvals)
        self.assertEqual((effective.max_nodes, effective.max_attempts), (20, 3))

    def test_network_process_and_hard_cap_escalation_are_rejected(self) -> None:
        proposals = (
            ExecutionPolicy(network_enabled=True),
            ExecutionPolicy(unrestricted_process_enabled=True),
            ExecutionPolicy(max_nodes=DEFAULT_SAFETY_FLOOR.max_nodes + 1),
        )
        for proposal in proposals:
            with self.subTest(proposal=proposal):
                with self.assertRaises(PolicyCompositionError) as raised:
                    compose_execution_policy(proposal)
                self.assertEqual(raised.exception.failure.kind, FailureKind.POLICY)
                self.assertFalse(raised.exception.failure.retryable)

    def test_supplied_floor_cannot_weaken_the_runtime_baseline(self) -> None:
        weaker_floors = (
            SafetyPolicyFloor(forbidden_capabilities=()),
            SafetyPolicyFloor(mandatory_approvals=()),
            SafetyPolicyFloor(max_nodes=DEFAULT_SAFETY_FLOOR.max_nodes + 1),
            SafetyPolicyFloor(network_enabled=True),
        )
        for floor in weaker_floors:
            with self.subTest(floor=floor):
                with self.assertRaises(PolicyCompositionError) as raised:
                    compose_execution_policy(floor=floor)
                self.assertEqual(raised.exception.failure.code, "runtime_floor_violation")

    def test_supplied_floor_may_only_add_restrictions(self) -> None:
        floor = SafetyPolicyFloor(
            forbidden_capabilities=(
                *DEFAULT_SAFETY_FLOOR.forbidden_capabilities,
                "filesystem.write",
            ),
            mandatory_approvals=(
                *DEFAULT_SAFETY_FLOOR.mandatory_approvals,
                "artifact.export",
            ),
            max_nodes=20,
        )
        effective = compose_execution_policy(floor=floor)
        self.assertIn("filesystem.write", effective.denied_capabilities)
        self.assertIn("artifact.export", effective.required_approvals)
        self.assertEqual(effective.max_nodes, 20)


if __name__ == "__main__":
    unittest.main()
