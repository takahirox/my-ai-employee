"""Versioned invocation contracts shared by generation, validation and repair.

The ordinary model types remain the authority for shape/local definitions. This
binding supplies only runtime-owned references and stage-specific constraints.
It never turns an evaluation target into accepted execution input.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import ValidationError

from .diagnostics import capture, redact
from .models import (
    CLARIFICATION_RULES,
    Clarification,
    Contract,
    Plan,
    RunConfig,
    Usage,
    Verification,
    WorkerChoice,
    WorkerResult,
)
from .semantics import RULES, external_evidence, projection
from .source_refs import original_source, resolve_source_refs

VERSION = "stage-contract-5"
T = TypeVar("T", bound=Contract)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class OutputViolation(ValueError):
    """Only response faults enter output repair; never arbitrary adapter errors."""

    def __init__(self, code: str, usage: Usage | None = None, *, details: Any = None) -> None:
        super().__init__(code)
        self.usage = usage or Usage()
        self.details = details


def validation_code(error: ValidationError) -> str:
    # Never include Pydantic's input values, arbitrary validator text or locations
    # controlled by a model. A bounded type-only finding is safe repair feedback.
    known = RULES.keys() | CLARIFICATION_RULES.keys()
    for item in error.errors(include_input=False, include_context=False, include_url=False):
        message = item["msg"].removeprefix("Value error, ")
        if message in known:
            return message
    return "INVALID_STRUCTURED_OUTPUT"


def repair_feedback(code: str, details: Any) -> dict[str, Any]:
    """Project rules and safe context, never rejected proposals or authority objects."""
    from .capabilities import AUTHORITY_RULES

    rule = (RULES | AUTHORITY_RULES | CLARIFICATION_RULES).get(code, {})
    allowed = {
        "path",
        "expected",
        "received",
        "errors",
        "task",
        "criterion",
        "index",
        "unsupported_fields",
        "mandatory_checks",
        "unregistered_checks",
        "required_evidence_kind",
        "expected_count",
        "received_count",
    }
    context_truncated = False

    def bound(value: Any, depth: int = 0) -> Any:
        nonlocal context_truncated
        if depth > 4:
            context_truncated = True
            return "[omitted]"
        if isinstance(value, str):
            context_truncated |= len(value) > 256
            return value[:256]
        if isinstance(value, (list, tuple)):
            context_truncated |= len(value) > 32
            return [bound(item, depth + 1) for item in value[:32]]
        if isinstance(value, dict):
            context_truncated |= len(value) > 8
            return {key: bound(item, depth + 1) for key, item in list(value.items())[:8]}
        return value if value is None or isinstance(value, (bool, int, float)) else "[omitted]"

    selected = (
        {key: value for key, value in details.items() if key in allowed}
        if isinstance(details, dict)
        else {}
    )
    sanitized, _ = redact(selected)
    context = {key: bound(value) for key, value in sanitized.items()}
    if context_truncated:
        context.update(
            context_truncated=True,
            reference_source="Use the complete StageContract "
            "and provider schema reference sets; this diagnostic context is partial.",
        )
    record = capture({**context, **rule, "authoritative": False}, 8192)
    if record["truncated"]:
        return {
            **rule,
            "authoritative": False,
            "context_excerpt": record["text"],
            "truncated": True,
        }
    result: dict[str, Any] = json.loads(record["text"])
    return result


def validation_details(
    error: ValidationError, payload: Any, schema: type[Contract]
) -> dict[str, Any]:
    # Unknown top-level fields are not a proposal. Do not retain their arbitrary values.
    fields = schema.model_fields
    selected = (
        {k: v for k, v in payload.items() if k in fields} if isinstance(payload, dict) else None
    )
    return {
        "payload": selected,
        "omitted_unknown_fields": len(set(payload) - set(fields))
        if isinstance(payload, dict)
        else 0,
        "non_object_payload_omitted": not isinstance(payload, dict),
        "errors": [
            {"path": e["loc"], "type": e["type"]}
            for e in error.errors(include_input=False, include_context=False, include_url=False)
        ],
    }


@dataclass(frozen=True)
class StageContract:
    stage: str
    checks: tuple[str, ...]
    criteria: tuple[str, ...] | None
    context_digest: str
    target_digest: str | None
    policy_digest: str
    authority: dict[str, object]
    constraints: dict[str, Any]
    original_input: str | None

    @classmethod
    def bind(
        cls,
        stage: str,
        prompt: dict[str, Any],
        config: RunConfig,
        *,
        original_input: str | None = None,
    ) -> StageContract:
        from .capabilities import authority_projection

        context = {k: v for k, v in prompt.items() if k not in {"feedback", "review_feedback"}}
        criteria = None
        if stage.endswith("_review"):
            criteria = ("review",)
        elif "criteria" in prompt:
            criteria = tuple(item["id"] for item in prompt["criteria"])
        target = prompt.get("proposal", prompt.get("candidate"))
        constraints: dict[str, Any] = {"mandatory_checks": config.mandatory_checks}
        source = prompt.get("original", prompt) if stage.endswith("_review") else prompt
        if stage in {"selection", "selection_review"}:
            constraints["maximum_worker_index"] = len(source["options"]) - 1
        if stage in {"recovery", "recovery_review"}:
            historical = {task["id"] for task in source.get("historical_tasks", [])}
            original = set(source.get("original_task_ids", historical))
            constraints.update(
                historical_tasks=tuple(sorted(historical)),
                remaining_added_tasks=config.limits.added_tasks - len(historical - original),
                failed_tasks=tuple(source.get("failed_tasks", ())),
            )
        return cls(
            stage,
            tuple(check.id for check in config.checks),
            criteria,
            digest(context),
            None if target is None else digest(target),
            config.digest,
            authority_projection(config),
            constraints,
            original_input if original_input is not None else source.get("original_input"),
        )

    def projection(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "stage": self.stage,
            "checks": self.checks,
            "criteria": self.criteria,
            "context_digest": self.context_digest,
            "evaluation_target_digest": self.target_digest,
            "policy_digest": self.policy_digest,
            "authority": self.authority,
            "semantics": projection(self.stage),
            **(
                {"original_source": original_source(self.original_input)}
                if self.original_input is not None
                else {}
            ),
            "constraints": self.constraints,
            **(
                {"clarification": Clarification.semantics()}
                if self.stage in {"clarification", "clarification_review"}
                else {}
            ),
            "reference_set_digest": digest({"checks": self.checks, "criteria": self.criteria}),
            "local_references": "Define criterion/task IDs locally; reference only definitions in "
            "this proposal. Existing task definitions must remain exact. Runtime identities, "
            "Goal criteria and policy cannot be changed. Evaluation targets have no authority.",
        }

    @property
    def identity(self) -> str:
        return digest(self.projection())

    def validate(self, value: T, prompt: dict[str, Any], config: RunConfig) -> T:
        if isinstance(value, Clarification):
            for name in ("requirements", "unresolved"):
                for index, item in enumerate(getattr(value, name)):
                    try:
                        resolve_source_refs(self.original_input or "", item.original_refs)
                    except ValueError as error:
                        raise OutputViolation(
                            "INVALID_ORIGINAL_REFERENCES",
                            details={"path": f"{name}.original_refs", "index": index},
                        ) from error
            for index, need in enumerate(value.unresolved):
                if need.action == "repair":
                    raise OutputViolation(
                        "CLARIFICATION_REQUIRES_INVESTIGATION",
                        details={
                            **CLARIFICATION_RULES["CLARIFICATION_REQUIRES_INVESTIGATION"],
                            "index": index,
                        },
                    )
            self._checks(value.criteria)
            if not set(config.mandatory_checks) <= {
                c for item in value.criteria for c in item.checks
            }:
                raise OutputViolation(
                    "MANDATORY_CHECK_OMITTED",
                    details={
                        "path": "criteria.checks",
                        "mandatory_checks": config.mandatory_checks,
                    },
                )
            self._outcomes(value.criteria, config)
        if isinstance(value, Plan):
            from .capabilities import AUTHORITY_RULES, task_violation

            external_checks = {
                check
                for task in value.tasks
                for criterion in task.criteria
                if criterion.requires_external_evidence
                for check in criterion.checks
            }
            for criterion in prompt.get("goal", {}).get("specification", {}).get("criteria", []):
                if (
                    external_evidence(criterion["outcome"])
                    and not set(criterion["checks"]) <= external_checks
                ):
                    raise OutputViolation(
                        "EXTERNAL_GOAL_WEAKENED",
                        details={
                            **AUTHORITY_RULES["EXTERNAL_GOAL_WEAKENED"],
                            "criterion": criterion["id"],
                        },
                    )
            historical = {t["id"]: t for t in prompt.get("historical_tasks", [])}
            if self.stage == "recovery":
                added = {t.id for t in value.tasks} - historical.keys()
                if not added or len(added) > self.constraints["remaining_added_tasks"]:
                    raise OutputViolation(
                        "GRAPH_GROWTH_LIMIT",
                        details={
                            "expected": {
                                "minimum_new_tasks": 1,
                                "maximum_new_tasks": self.constraints["remaining_added_tasks"],
                            },
                            "received": len(added),
                        },
                    )
            for task in value.tasks:
                self._checks(task.criteria)
                violation = task_violation(task, config)
                if violation:
                    raise OutputViolation(str(violation["reason"]), details=violation)
                if task.id in historical and task.model_dump(mode="json") != historical[task.id]:
                    raise OutputViolation("HISTORICAL_TASK_REWRITTEN", details={"task": task.id})
                if task.supersedes is not None and task.supersedes not in historical:
                    raise OutputViolation(
                        "FOREIGN_REPAIR_TARGET",
                        details={
                            "task": task.id,
                            "received": task.supersedes,
                            "expected": sorted(historical),
                        },
                    )
                failed = set(task.dependencies) & set(self.constraints.get("failed_tasks", ()))
                if failed:
                    raise OutputViolation(
                        "FAILED_DEPENDENCY",
                        details={
                            "task": task.id,
                            "received": sorted(failed),
                        },
                    )
        if isinstance(value, Verification) and self.criteria is not None:
            ids = [f.criterion_id for f in value.findings]
            if len(ids) != len(self.criteria) or set(ids) != set(self.criteria):
                raise OutputViolation(
                    "INVALID_FINDING_REFERENCES",
                    details={
                        "path": "findings.criterion_id",
                        "expected": self.criteria,
                        "received": ids,
                        "expected_count": len(self.criteria),
                        "received_count": len(ids),
                    },
                )
        if (
            isinstance(value, WorkerChoice)
            and value.index > self.constraints["maximum_worker_index"]
        ):
            raise OutputViolation(
                "WORKER_SELECTION_OUT_OF_RANGE",
                details={
                    "received": value.index,
                    "expected": {"minimum": 0, "maximum": self.constraints["maximum_worker_index"]},
                },
            )
        if isinstance(value, WorkerResult) and value.authority_request is not None:
            from .capabilities import (
                authority_policy_failure,
                authority_support_failure,
                task_violation,
            )
            from .models import Task

            reason = authority_policy_failure(
                value.authority_request, config
            ) or authority_support_failure(value.authority_request)
            if reason:
                raise OutputViolation(reason, details={"path": "authority_request"})
            if "context" in prompt:
                task = Task.model_validate(prompt["context"]["task"]).model_copy(
                    update={"authority": value.authority_request}
                )
                violation = task_violation(task, config)
                if violation:
                    raise OutputViolation(str(violation["reason"]), details=violation)
        return value

    def _checks(self, criteria: Any) -> None:
        for item in criteria:
            unknown = sorted(set(item.checks) - set(self.checks))
            if unknown:
                raise OutputViolation(
                    "UNDECLARED_VERIFICATION_CHECK",
                    details={
                        "path": "criteria.checks",
                        "criterion": item.id,
                        "unregistered_checks": unknown,
                    },
                )

    @staticmethod
    def _outcomes(criteria: Any, config: RunConfig) -> None:
        external = {c.id for c in config.checks if c.proves_external_effect}
        for criterion in criteria:
            if criterion.requires_external_evidence and not set(criterion.checks) & external:
                raise OutputViolation(
                    "MISSING_EXTERNAL_EVIDENCE_CHECK",
                    details={
                        "path": "criteria.checks",
                        "criterion": criterion.id,
                        "required_evidence_kind": "external_effect",
                    },
                )
