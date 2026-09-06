"""Opt-in, request-specific acceptance using existing Harness command authority."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .domain import CompletionCriterion, Goal, GoalTaskKind, ProjectHarnessV2
from .domain.base import Identifier
from .domain.harness import HarnessEvaluator

PREFIX = "goal.acceptance."


class GoalCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: Identifier = Field(max_length=128 - len(PREFIX))
    request_fragment: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=1200)
    command_ref: Identifier


class GoalChecks(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["1"]
    goal: str = Field(min_length=1, max_length=10_000)
    criteria: tuple[GoalCheck, ...] = Field(min_length=1, max_length=16)


def attach_goal_checks(goal: Goal, path: str | Path) -> Goal:
    """Read explicit operator intent before dispatch; never use worker-authored checks."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 32_000:
        raise ValueError("goal acceptance file must be a regular file of at most 32000 bytes")
    plan = GoalChecks.model_validate_json(path.read_bytes(), strict=True)
    if plan.goal != goal.statement or goal.task_kind is not GoalTaskKind.MUTATING:
        raise ValueError("acceptance criteria require this exact mutating Goal")
    criteria = list(goal.completion_criteria)
    for check in plan.criteria:
        if not check.request_fragment.strip() or check.request_fragment not in goal.statement:
            raise ValueError("criterion must cite an exact nonblank fragment of the original Goal")
        criterion_id = PREFIX + check.id
        if any(c.id == criterion_id for c in criteria):
            raise ValueError("goal acceptance criterion IDs must be unique")
        criteria.append(
            CompletionCriterion(
                id=criterion_id,
                description=f"{check.description} [request: {check.request_fragment}]",
                verification_requirement_ids=(check.command_ref,),
                required_artifact_ids=("workspace_patch",),
            )
        )
    return Goal.model_validate(
        {**goal.model_dump(), "completion_criteria": tuple(criteria)}, strict=True
    )


def harness_for_goal(harness: ProjectHarnessV2, goal: Goal) -> ProjectHarnessV2:
    """Reconstruct the same effective checks on execution, resume and promotion.

    First milestone accepts only inline Python checks already declared in Harness.
    This freezes test logic in the captured command, not a candidate-writable test
    file. This is not a proof of semantic adequacy; the operator supplies that intent.
    """
    checks = [c for c in goal.completion_criteria if c.id.startswith(PREFIX)]
    if not checks:
        return harness
    if goal.task_kind is not GoalTaskKind.MUTATING or not goal.processes_authorized:
        raise ValueError("goal acceptance checks require authorized verification processes")
    evaluators = list(harness.evaluators)
    required = list(harness.verification.required)
    required_evaluators = list(harness.verification.required_evaluators)
    for criterion in checks:
        if not criterion.mandatory or len(criterion.verification_requirement_ids) != 1:
            raise ValueError("goal acceptance criterion cannot be weakened")
        name = criterion.verification_requirement_ids[0]
        command = harness.commands.get(name)
        if command is None:
            raise ValueError("goal acceptance command must already be declared in Harness")
        argv = command.argv
        if (
            len(argv) != 4
            or argv[1:3] != ("-I", "-c")
            or not Path(argv[0]).name.startswith("python")
            or command.inherit_environment
        ):
            raise ValueError(
                "initial goal checks require declared python -I -c with no inherited environment"
            )
        if name not in required:
            required.append(name)
        evaluator = HarnessEvaluator(
            id=criterion.id,
            provider_id="process.harness",
            command_ref=name,
            criterion_ids=(criterion.id,),
        )
        if any(e.id == evaluator.id or criterion.id in e.criterion_ids for e in evaluators):
            raise ValueError("goal acceptance evaluator conflicts with Harness declarations")
        evaluators.append(evaluator)
        required_evaluators.append(evaluator.id)
    payload = harness.model_dump()
    payload.update(
        evaluators=tuple(evaluators),
        verification={
            **harness.verification.model_dump(),
            "required": tuple(required),
            "required_evaluators": tuple(required_evaluators),
        },
    )
    return ProjectHarnessV2.model_validate(payload, strict=True)
