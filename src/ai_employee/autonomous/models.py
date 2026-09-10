"""Small immutable stage contracts; transport output never owns runtime identity."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=20000)]
Key = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    def canonical(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()


class Criterion(Contract):
    id: Key
    description: Text
    # Check IDs resolve only against operator-owned definitions in RunConfig.
    checks: tuple[Key, ...] = ()


class Requirement(Contract):
    original_fragment: Text
    criteria: tuple[Key, ...] = Field(min_length=1)


class Clarification(Contract):
    clarified_goal: Text
    criteria: tuple[Criterion, ...] = Field(min_length=1)
    requirements: tuple[Requirement, ...] = Field(min_length=1)
    assumptions: tuple[Text, ...] = ()
    unresolved: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def unique_criteria(self) -> Self:
        ids = {item.id for item in self.criteria}
        if len(ids) != len(self.criteria):
            raise ValueError("DUPLICATE_CRITERION")
        if any(not set(item.criteria) <= ids for item in self.requirements):
            raise ValueError("FOREIGN_REQUIREMENT_CRITERION")
        return self


class Goal(Contract):
    original_input: Text
    specification: Clarification
    mandatory_checks: tuple[Key, ...] = ()

    @model_validator(mode="after")
    def accepted(self) -> Self:
        if self.specification.unresolved:
            raise ValueError("WAITING_FOR_CLARIFICATION")
        if any(
            item.original_fragment not in self.original_input
            for item in self.specification.requirements
        ):
            raise ValueError("FOREIGN_REQUIREMENT_FRAGMENT")
        checks = {check for item in self.specification.criteria for check in item.checks}
        if not set(self.mandatory_checks) <= checks:
            raise ValueError("MANDATORY_CHECK_OMITTED")
        return self


class Authority(Contract):
    network_hosts: tuple[Text, ...] = ()
    credentials: tuple[Key, ...] = ()
    external_writes: bool = False
    operation_approval: bool = False
    duplicate_prevention: bool = False

    def within(self, ceiling: Authority) -> bool:
        return (
            set(self.network_hosts) <= set(ceiling.network_hosts)
            and set(self.credentials) <= set(ceiling.credentials)
            and (not self.external_writes or ceiling.external_writes)
        )


class Task(Contract):
    id: Key
    description: Text
    criteria: tuple[Criterion, ...] = Field(min_length=1)
    verification_plan: Text
    required_evidence: tuple[Text, ...] = ()
    dependencies: tuple[Key, ...] = ()
    authority: Authority = Authority()
    kind: Literal["work", "integration", "repair"] = "work"
    supersedes: Key | None = None

    @model_validator(mode="after")
    def unique_bindings(self) -> Self:
        if len({criterion.id for criterion in self.criteria}) != len(self.criteria):
            raise ValueError("DUPLICATE_TASK_CRITERION")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("DUPLICATE_DEPENDENCY")
        return self


class Plan(Contract):
    tasks: tuple[Task, ...] = Field(min_length=1, max_length=256)
    result_task: Key

    @model_validator(mode="after")
    def dag(self) -> Self:
        tasks = {task.id: task for task in self.tasks}
        if len(tasks) != len(self.tasks) or self.result_task not in tasks:
            raise ValueError("INVALID_GRAPH_IDENTITY")
        pending = set(tasks)
        resolved: set[str] = set()
        while pending:
            ready = {key for key in pending if set(tasks[key].dependencies) <= resolved}
            if not ready:
                raise ValueError("CYCLIC_OR_MISSING_DEPENDENCY")
            resolved.update(ready)
            pending.difference_update(ready)
        # Every planned task must contribute to the adopted final result.
        ancestors = {self.result_task}
        frontier = [self.result_task]
        while frontier:
            for parent in tasks[frontier.pop()].dependencies:
                if parent not in ancestors:
                    ancestors.add(parent)
                    frontier.append(parent)
        if ancestors != set(tasks):
            raise ValueError("UNCONNECTED_RESULT")
        return self


class Check(Contract):
    id: Key
    argv: tuple[Text, ...] = Field(min_length=1)
    timeout: float = Field(default=60, gt=0)


class StagePolicy(Contract):
    backend: Literal["codex", "claude"] = "codex"
    model: Text
    effort: Text = "high"
    review: Literal["never", "always", "conditional"] = "never"
    review_conditions: tuple[Literal["ambiguity", "external_authority"], ...] = ()
    reviewer_model: Text | None = None
    revisions: int = Field(default=1, ge=0, le=10)


class Limits(Contract):
    wall_seconds: float = Field(default=1800, gt=0)
    active_seconds: float = Field(default=1800, gt=0)
    invocation_seconds: float = Field(default=300, gt=0)
    tokens: int | None = Field(default=None, gt=0)
    cost: float | None = Field(default=None, gt=0)
    reservation_tokens: int = Field(default=100000, gt=0)
    reservation_cost: float = Field(default=10, gt=0)
    attempts: int = Field(default=32, gt=0)
    task_attempts: int = Field(default=3, gt=0)
    added_tasks: int = Field(default=16, ge=0)
    replans: int = Field(default=2, ge=0)
    concurrency: int = Field(default=2, gt=0, le=32)
    approval_counts_wall: bool = True


class RunConfig(Contract):
    schema_version: Literal["autonomous-1"] = "autonomous-1"
    clarification: StagePolicy
    planning: StagePolicy
    worker: StagePolicy
    worker_options: tuple[StagePolicy, ...] = ()
    selection: StagePolicy | None = None
    verification: StagePolicy
    recovery: StagePolicy
    limits: Limits = Limits()
    checks: tuple[Check, ...] = ()
    mandatory_checks: tuple[Key, ...] = ()
    authority_ceiling: Authority = Authority()
    security: Literal["strict", "balanced", "permissive"] = "strict"

    @model_validator(mode="after")
    def check_definitions(self) -> Self:
        keys = {check.id for check in self.checks}
        if len(keys) != len(self.checks) or not set(self.mandatory_checks) <= keys:
            raise ValueError("INVALID_CHECK_DEFINITIONS")
        return self


class Candidate(Contract):
    tree: Digest
    task_digest: Digest
    attempt_id: Key
    upstream: tuple[Digest, ...]
    authority_version: int = Field(ge=0)


class TaskContext(Contract):
    goal: Goal
    task: Task
    attempt_id: Key
    upstream: tuple[Candidate, ...]
    workspace: Text
    authority: Authority
    authority_version: int = Field(ge=0)


class WorkerResult(Contract):
    status: Literal["completed", "failed", "authority_requested", "uncertain", "usage_limit"]
    summary: Text
    evidence: tuple[Text, ...] = ()
    authority_request: Authority | None = None


class Finding(Contract):
    criterion_id: Key
    passed: bool
    evidence: Text


class Verification(Contract):
    findings: tuple[Finding, ...]
    summary: Text

    def accepts(self, criteria: tuple[Criterion, ...]) -> bool:
        return (
            len(self.findings) == len(criteria)
            and {item.criterion_id for item in self.findings} == {item.id for item in criteria}
            and all(item.passed for item in self.findings)
        )


class Usage(Contract):
    tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)


class WorkerChoice(Contract):
    index: int = Field(ge=0)
    reason: Text
