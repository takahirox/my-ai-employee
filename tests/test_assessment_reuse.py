from __future__ import annotations

from hashlib import sha256

import pytest

from ai_employee.domain import ExecutionStrategy, RoutingMode, SemanticTaskProfile
from ai_employee.domain.v2 import DecisionOutcome, ExecutionResult, PolicyDecision
from ai_employee.routing import assess_task, merge_semantic_profile
from ai_employee.run_budget import WallTimeExceeded
from ai_employee.worker_adapters import CliTaskAssessmentAdapter
from tests.test_work_orchestration_v2 import NOW, ZERO

PROFILE = b"""{"task_type":"mechanical","reasoning_class":"mechanical",
"scope":"bounded","ambiguity":"low","reasons":["one explicit operation"]}"""


class Execution:
    def __init__(self):
        self.calls = 0
        self.response = PROFILE

    def execute(self, request, decision, cancellation):
        self.calls += 1
        return ExecutionResult(
            id=f"execution-{self.calls}",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest,
            status="succeeded",
            duration_seconds=0.01,
            stdout_artifact_digest="1" * 64,
        )


class Policy:
    def __init__(self):
        self.calls = 0
        self.outcome = DecisionOutcome.ALLOW
        self.digest = ZERO
        self.foreign = False

    def __call__(self, request):
        self.calls += 1
        return PolicyDecision(
            id=f"policy-{self.calls}",
            run_id="foreign" if self.foreign else request.run_id,
            created_at=NOW,
            request_digest=request.content_digest,
            effective_policy_digest=self.digest,
            outcome=self.outcome,
            reason_code="fixture",
        )


def assessor(execution=None, policy=None):
    execution = execution or Execution()
    policy = policy or Policy()
    adapter = CliTaskAssessmentAdapter(
        execution,
        lambda _: execution.response,
        policy,
        run_id="run-1",
        strategy=ExecutionStrategy(
            id="classifier",
            routing_mode=RoutingMode.ADAPTIVE,
            backend="codex_cli",
            model="gpt-5.6-sol",
            effort="high",
        ),
        executable="codex",
        cwd=".",
        prompt_writer=lambda value: sha256(value).hexdigest(),
        output_schema_path="schema.json",
    )
    return adapter, execution, policy


def deterministic(**changes):
    return assess_task("Sort values", run_id="node-1", risk=0).model_copy(update=changes)


def test_identical_input_reuses_valid_profile_but_preserves_current_routing_facts():
    adapter, execution, policy = assessor()
    first = adapter.assess("Sort values", deterministic())
    polls = []
    current = deterministic(risk=9, required_capabilities=("download",))
    second = adapter.assess_supervised("Sort values", current, on_poll=lambda: polls.append(True))
    assert first == second == SemanticTaskProfile.model_validate_json(PROFILE)
    assert execution.calls == 1 and policy.calls == 2 and polls == [True]
    merged = merge_semantic_profile(current, second)
    assert merged.risk == 9 and merged.required_capabilities == ("download",)


@pytest.mark.parametrize("change", ["goal", "context", "run", "strategy", "executable", "policy"])
def test_changed_classifier_input_or_scope_requires_a_new_call(change):
    adapter, execution, policy = assessor()
    adapter.assess("Sort values", deterministic())
    goal = "Sort values"
    assessment = deterministic()
    if change == "goal":
        goal = "Sort other values"
    elif change == "context":
        assessment = deterministic(context_character_count=100)
    elif change == "run":
        adapter.run_id = "run-2"
    elif change == "strategy":
        adapter.strategy = adapter.strategy.model_copy(update={"effort": "medium"})
    elif change == "executable":
        adapter.executable = "other-codex"
    else:
        policy.digest = "2" * 64
    adapter.assess(goal, assessment)
    assert execution.calls == 2


def test_reuse_is_local_and_bounded_to_last_success():
    adapter, execution, policy = assessor()
    for goal in ("Sort values", "Reverse values", "Sort values"):
        adapter.assess(goal, deterministic())
    assert execution.calls == 3
    other, _, _ = assessor(execution, policy)
    other.assess("Sort values", deterministic())
    assert execution.calls == 4


@pytest.mark.parametrize("failure", ["denied", "foreign", "expected_policy", "owner", "deadline"])
def test_reuse_cannot_bypass_policy_owner_or_deadline(failure, monkeypatch):
    adapter, execution, policy = assessor()
    adapter.assess("Sort values", deterministic())

    def poll():
        if failure == "owner":
            raise ValueError("owner lost")

    if failure == "denied":
        policy.outcome = DecisionOutcome.DENY
    elif failure == "foreign":
        policy.foreign = True
    elif failure == "expected_policy":
        adapter.expected_effective_policy_digest = "3" * 64
    elif failure == "deadline":

        def expired():
            raise WallTimeExceeded("run-1")

        monkeypatch.setattr("ai_employee.worker_adapters.check_wall_budget", expired)
    with pytest.raises(WallTimeExceeded if failure == "deadline" else ValueError):
        adapter.assess_supervised("Sort values", deterministic(), on_poll=poll)
    assert execution.calls == 1


def test_invalid_model_output_is_not_reused():
    adapter, execution, _ = assessor()
    execution.response = b"{}"
    with pytest.raises(ValueError, match="invalid semantic task assessment"):
        adapter.assess("Sort values", deterministic())
    execution.response = PROFILE
    adapter.assess("Sort values", deterministic())
    assert execution.calls == 2
