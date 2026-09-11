"""Small immutable stage contracts; transport output never owns runtime identity."""

from __future__ import annotations

import hashlib
import json
from fnmatch import fnmatchcase
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .isolated_worker import IsolatedWorkerProfile
from .product_capabilities import SUPPORTED_BACKENDS

Text = Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]
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
    outcome: Literal["artifact", "external_effect"] = "artifact"


class Requirement(Contract):
    original_fragment: Text
    criteria: tuple[Key, ...] = Field(min_length=1)


# One semantic definition for schema, stage context, review, repair and runtime decisions.
CLARIFICATION_NEEDS = {
    "human_input": {
        "action": "wait",
        "rule": "A concrete user decision or additional information is required by the original "
        "request and cannot be obtained from permitted inputs. Ask a specific question and "
        "explain the investigation/evidence. Missing input belongs here only when the user "
        "must supply it; do not invent out-of-scope requirements.",
    },
    "investigation": {
        "action": "repair",
        "rule": "Inspect permitted inputs and repair this proposal within the existing revision "
        "budget. Unknown columns, values or schema are not human questions. Ordinary later "
        "work belongs in the goal/criteria or assumptions, not unresolved.",
    },
    "environment": {
        "action": "stop",
        "rule": "Report an input/access/environment blocker with observed evidence after "
        "checking the supplied snapshot and actual execution workspace. This does not ask "
        "the user to decide intent, grant authority, or waive isolation. A reported blocker "
        "stops execution; model claims are not independently proven environment facts.",
    },
}
CLARIFICATION_ACTION_ORDER = ("repair", "stop", "wait", "proceed")
CLARIFICATION_RULES = {
    "CLARIFICATION_QUESTION_ACTION_MISMATCH": {
        "path": "unresolved.question",
        "rule": "A specific question for the user is required only for human_input; "
        "question must be null for investigation or environment.",
    },
    "CLARIFICATION_REQUIRES_INVESTIGATION": {
        "path": "unresolved",
        "rule": CLARIFICATION_NEEDS["investigation"]["rule"],
    },
    "FOREIGN_CLARIFICATION_REFERENCE": {
        "path": "unresolved.original_fragment",
        "rule": "Each unresolved need must cite an exact fragment of the original request. "
        "Do not add questions for excluded or unrequested cases.",
    },
}


class ClarificationNeed(Contract):
    kind: Literal["human_input", "investigation", "environment"] = Field(
        description=json.dumps(CLARIFICATION_NEEDS, sort_keys=True)
    )
    question: Text | None = Field(
        description=CLARIFICATION_RULES["CLARIFICATION_QUESTION_ACTION_MISMATCH"]["rule"]
    )
    reason: Text = Field(description="Why this need blocks the original request.")
    original_fragment: Text = Field(
        description=CLARIFICATION_RULES["FOREIGN_CLARIFICATION_REFERENCE"]["rule"]
    )
    evidence: Text = Field(
        description="Observed permitted-input investigation or facts supporting this need. "
        "Distinguish inspected facts from assumptions; never claim an unperformed inspection."
    )

    @property
    def action(self) -> str:
        return CLARIFICATION_NEEDS[self.kind]["action"]

    @model_validator(mode="after")
    def question_matches_action(self) -> Self:
        if (self.action == "wait") != (self.question is not None):
            raise ValueError("CLARIFICATION_QUESTION_ACTION_MISMATCH")
        return self


class Clarification(Contract):
    clarified_goal: Text = Field(description="Faithful goal preserving the original request.")
    criteria: tuple[Criterion, ...] = Field(
        min_length=1, description="Checkable success criteria, preserving required outcome kinds."
    )
    requirements: tuple[Requirement, ...] = Field(
        min_length=1, description="Exact original fragments mapped to their success criteria."
    )
    assumptions: tuple[Text, ...] = Field(
        default=(),
        description="Disclosed interpretations and ordinary deferred work; "
        "never hide a necessary user decision or waive requirements here.",
    )
    unresolved: tuple[ClarificationNeed, ...] = Field(
        default=(),
        description="Blocking needs with explicit resolution semantics. Empty when "
        "the goal can proceed. Nonempty does not by itself authorize a human wait.",
    )

    @property
    def disposition(self) -> str:
        actions = {need.action for need in self.unresolved}
        # Investigation must finish before deciding whether remaining needs require
        # a stop or a human answer. Environment blockers take precedence over waiting.
        return next(
            (action for action in CLARIFICATION_ACTION_ORDER if action in actions), "proceed"
        )

    @classmethod
    def semantics(cls) -> dict[str, object]:
        return {
            "fields": {name: field.description for name, field in cls.model_fields.items()},
            "needs": CLARIFICATION_NEEDS,
            "violations": CLARIFICATION_RULES,
            "precedence": CLARIFICATION_ACTION_ORDER,
            "review": "Review both the goal and every unresolved need against the original "
            "request and permitted inputs. Reject misclassified, unsupported or unnecessary "
            "human questions. Approval cannot convert investigation into a human wait.",
        }

    @model_validator(mode="after")
    def unique_criteria(self) -> Self:
        ids = {item.id for item in self.criteria}
        if len(ids) != len(self.criteria):
            raise ValueError("DUPLICATE_CRITERION")
        mapped = {key for item in self.requirements for key in item.criteria}
        if not mapped <= ids:
            raise ValueError("FOREIGN_REQUIREMENT_CRITERION")
        if mapped != ids:
            raise ValueError("UNMAPPED_CRITERION")
        return self


class Goal(Contract):
    original_input: Text
    specification: Clarification
    mandatory_checks: tuple[Key, ...] = ()

    @model_validator(mode="after")
    def accepted(self) -> Self:
        if self.specification.disposition != "proceed":
            raise ValueError(
                "WAITING_FOR_CLARIFICATION"
                if self.specification.disposition == "wait"
                else "CLARIFICATION_NOT_READY"
            )
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
    network_hosts: tuple[Text, ...] = Field(
        default=(),
        description="HTTPS destination host grants; not URLs or per-operation permissions.",
    )
    credentials: tuple[Key, ...] = Field(
        default=(),
        description="Named service credentials required by the task, not model authentication.",
    )
    external_writes: bool = Field(
        default=False,
        description="Requires a write-capable external grant; "
        "local workspace writes do not require this.",
    )
    operation_approval: bool = Field(
        default=False, description="Requires enforced approval before each external operation."
    )
    duplicate_prevention: bool = Field(
        default=False,
        description="Requires enforced prevention of duplicate external side effects; "
        "unrelated to deduplicating local data.",
    )

    @model_validator(mode="after")
    def resource_names(self) -> Self:
        import re

        if len(set(self.credentials)) != len(self.credentials) or len(
            set(self.network_hosts)
        ) != len(self.network_hosts):
            raise ValueError("DUPLICATE_AUTHORITY_RESOURCE")
        for host in self.network_hosts:
            if (
                not re.fullmatch(r"(?:\*|(?:\*\.)?[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?)", host)
                or ".." in host
            ):
                raise ValueError("AUTHORITY_REQUIRES_HOST_NAMES_NOT_URLS_OR_PORTS")
        return self

    def within(self, ceiling: Authority) -> bool:
        return (
            all(
                any(fnmatchcase(host, pattern) for pattern in ceiling.network_hosts)
                for host in self.network_hosts
            )
            and set(self.credentials) <= set(ceiling.credentials)
            and (not self.external_writes or ceiling.external_writes)
            and (
                not self.external_writes
                or not ceiling.operation_approval
                or self.operation_approval
            )
            and (
                not self.external_writes
                or not ceiling.duplicate_prevention
                or self.duplicate_prevention
            )
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
    evidence_kind: Literal["artifact", "external_effect"] = "artifact"


class StagePolicy(Contract):
    backend: Literal["codex", "claude"] = Field(
        default="codex",
        description="Only backends in x-supported-backends are runnable.",
        json_schema_extra={"x-supported-backends": list(SUPPORTED_BACKENDS)},
    )
    model: Text
    effort: Text = "high"
    review: Literal["never", "always", "conditional"] = "never"
    review_conditions: tuple[Literal["ambiguity", "external_authority"], ...] = ()
    reviewer_model: Text | None = None
    reviewer_backend: Literal["codex", "claude"] | None = None
    reviewer_effort: Text | None = None
    revisions: int = Field(default=1, ge=0, le=10)
    transport_retries: int = Field(default=0, ge=0, le=10)
    supervision_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def conditional_review(self) -> Self:
        if self.review == "conditional" and not self.review_conditions:
            raise ValueError("CONDITIONAL_REVIEW_REQUIRES_CONDITIONS")
        return self


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
    worker_escalations: tuple[StagePolicy, ...] = ()
    selection: StagePolicy | None = None
    verification: StagePolicy
    recovery: StagePolicy
    limits: Limits = Limits()
    checks: tuple[Check, ...] = ()
    mandatory_checks: tuple[Key, ...] = ()
    authority_ceiling: Authority = Authority()
    isolation: IsolatedWorkerProfile | None = None
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
    category: Literal[
        "satisfied",
        "missing_evidence",
        "omitted_requirement",
        "weakened_requirement",
        "unsupported_expansion",
        "incorrect_result",
        "unspecified",
    ] = "unspecified"


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
