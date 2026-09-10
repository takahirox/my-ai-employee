"""Independent review, selection and repair obey the snapshotted stage policies."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ai_employee.models import (
    Authority,
    Clarification,
    Criterion,
    Finding,
    StagePolicy,
    Usage,
    Verification,
    WorkerChoice,
)
from ai_employee.native import T

from .test_autonomous_runtime import OfflineModel, clarification, config, runtime


class ReviewModel(OfflineModel):
    def __init__(self, *, reject: bool = False, repair: bool = False) -> None:
        super().__init__(fail_once=repair)
        self.reject = reject
        self.policies: list[StagePolicy] = []
        self.review_workspaces: list[Path] = []

    def generate(
        self,
        policy: StagePolicy,
        prompt: str,
        schema: type[T],
        workspace: Path,
        authority: Authority,
        timeout: float,
        cancelled: Callable[[], bool],
        observer: Callable[[float, int], None] | None = None,
        observation: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[T, Usage]:
        body = json.loads(prompt)
        self.policies.append(policy)
        if schema is WorkerChoice:
            return schema.model_validate(
                WorkerChoice(index=1, reason="configured option").model_dump()
            ), Usage(tokens=1, cost=0)
        if schema is Verification and "proposal" in body:
            self.review_workspaces.append(workspace)
            # Verifier-authored files are absent from the independent review copy.
            assert not (workspace / "verifier-output.txt").exists()
            result = Verification(
                findings=(
                    Finding(
                        criterion_id="review",
                        passed=not self.reject,
                        evidence="compared exact original",
                    ),
                ),
                summary="independent review",
            )
            return schema.model_validate(result.model_dump()), Usage(tokens=1, cost=0)
        return super().generate(policy, prompt, schema, workspace, authority, timeout, cancelled)


def test_rejected_clarification_cannot_plan_after_revision_limit(tmp_path: Path) -> None:
    model = ReviewModel(reject=True)
    engine, source = runtime(tmp_path, model)
    configured = config().model_copy(
        update={"clarification": StagePolicy(model="clarifier", review="always", revisions=1)}
    )
    with pytest.raises(ValueError, match="CLARIFICATION_REJECTED"):
        engine.start("Write result", configured, source)
    assert model.calls == ["Clarification", "Clarification"]
    assert model.workers == 0 and len(model.review_workspaces) == 2


def test_selection_and_verifier_review_use_configured_models_and_isolated_copies(
    tmp_path: Path,
) -> None:
    model = ReviewModel()
    engine, source = runtime(tmp_path, model)
    reviewed = StagePolicy(
        model="verifier", review="always", reviewer_model="independent", reviewer_effort="medium"
    )
    configured = config().model_copy(
        update={
            "worker_options": (StagePolicy(model="first"), StagePolicy(model="chosen")),
            "selection": reviewed,
            "verification": reviewed,
        }
    )
    run = engine.start("Write result", configured, source)
    assert engine.journal.events(run)[-1]["kind"] == "completed"
    selected = [
        event["body"]["policy"]
        for event in engine.journal.events(run)
        if event["kind"] == "worker_selected"
    ]
    assert selected[0]["model"] == "chosen"
    assert any(
        policy.model == "independent" and policy.effort == "medium" for policy in model.policies
    )
    assert len(model.review_workspaces) == 3


def test_ordinary_verification_failure_uses_configured_escalation(tmp_path: Path) -> None:
    model = ReviewModel(repair=True)
    engine, source = runtime(tmp_path, model)
    configured = config().model_copy(
        update={"worker_escalations": (StagePolicy(model="stronger", effort="high"),)}
    )
    run = engine.start("Write result", configured, source)
    attempts = [
        event["body"] for event in engine.journal.events(run) if event["kind"] == "attempt_started"
    ]
    assert len(attempts) == 2
    assert attempts[1]["worker"]["model"] == "stronger"
    assert attempts[0]["context"]["workspace"] == attempts[1]["context"]["workspace"]


def test_unmapped_criteria_and_empty_conditional_review_are_rejected() -> None:
    with pytest.raises(ValueError, match="UNMAPPED_CRITERION"):
        Clarification.model_validate(
            {
                **clarification().model_dump(),
                "criteria": [
                    Criterion(id="result", description="result"),
                    Criterion(id="extra", description="unmapped expansion"),
                ],
            }
        )
    with pytest.raises(ValueError, match="CONDITIONAL_REVIEW_REQUIRES_CONDITIONS"):
        StagePolicy(model="fixture", review="conditional")


def test_wildcard_ceiling_allows_narrower_hosts_without_expanding_grant() -> None:
    ceiling = Authority(network_hosts=("*.example.com",), external_writes=True)
    assert Authority(network_hosts=("api.example.com",), external_writes=True).within(ceiling)
    assert not Authority(network_hosts=("example.com",), external_writes=True).within(ceiling)
    assert not Authority(network_hosts=("*",), external_writes=True).within(ceiling)
