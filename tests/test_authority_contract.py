"""Authority generation, deterministic admission, bounded repair and durable replay."""

from __future__ import annotations

import json

import pytest

from ai_employee.capabilities import (
    AUTHORITY_RULES,
    authority_projection,
    readiness,
    task_violation,
)
from ai_employee.engine import Engine
from ai_employee.history import Journal, Stopped
from ai_employee.models import Authority, Check, Criterion, Plan, StagePolicy, Task, Verification
from ai_employee.native import decode_response, provider_schema
from ai_employee.stage_contracts import OutputViolation, StageContract

from .test_autonomous_runtime import OfflineModel, config, runtime
from .test_stage_contracts import stream


class AuthorityModel(OfflineModel):
    def __init__(self, *, always=False, crash=False):
        super().__init__()
        self.prompts = []
        self.always, self.crash = always, crash

    def generate(self, policy, prompt, schema, *args, **kwargs):
        body = json.loads(prompt)
        if schema is Plan:
            if self.crash and self.prompts:
                # A launched repair consumes its reservation even without a response.
                self.crash = False
                raise SystemExit("controller lost")
            self.prompts.append(body)
        result, usage = super().generate(policy, prompt, schema, *args, **kwargs)
        if schema is Plan:
            payload = result.model_dump(mode="json")
            if len(self.prompts) == 1 or self.always:
                payload["tasks"][0]["authority"]["duplicate_prevention"] = True
            # Same completed-response decoder as the production native adapter.
            return decode_response(stream(payload, tokens=17), schema)
        return result, usage


def test_authority_semantics_project_into_schema_planner_review_and_recovery():
    cfg = config().model_copy(
        update={"authority_ceiling": Authority(network_hosts=("*.example.com",))}
    )
    projected = authority_projection(cfg)
    for stage, schema in (
        ("planning", Plan),
        ("recovery", Plan),
        ("planning_review", Verification),
    ):
        binding = StageContract.bind(stage, {}, cfg).projection()
        assert binding["authority"] == projected
        if schema is Plan:
            fields = provider_schema(schema, binding)["$defs"]["Authority"]["properties"]
            for name, constraints in projected["properties"].items():
                assert fields[name]["description"] == constraints["description"]
            assert "deduplicating local data" in fields["duplicate_prevention"]["description"]
            assert "Unsupported" in fields["duplicate_prevention"]["description"]
            # Infeasible requirements remain expressible, never coerced to false.
            assert "enum" not in fields["duplicate_prevention"]
    assert projected["authority_ceiling"]["network_hosts"] == ["*.example.com"]
    assert projected["rules"] == AUTHORITY_RULES


@pytest.mark.parametrize(
    "authority,security,ceiling,checks,reason",
    [
        (
            Authority(duplicate_prevention=True),
            "strict",
            Authority(),
            (),
            "REQUIRED_AUTHORITY_BOUNDARY_UNAVAILABLE",
        ),
        (
            Authority(operation_approval=True),
            "strict",
            Authority(),
            (),
            "REQUIRED_AUTHORITY_BOUNDARY_UNAVAILABLE",
        ),
        (
            Authority(credentials=("service",)),
            "balanced",
            Authority(credentials=("service",)),
            (),
            "REQUIRED_AUTHORITY_BOUNDARY_UNAVAILABLE",
        ),
        (
            Authority(network_hosts=("example.com",)),
            "balanced",
            Authority(network_hosts=("example.com",)),
            (),
            "READ_ONLY_NETWORK_BOUNDARY_UNAVAILABLE",
        ),
        (
            Authority(network_hosts=("other.com",)),
            "balanced",
            Authority(network_hosts=("example.com",)),
            (),
            "AUTHORITY_EXCEEDS_POLICY",
        ),
        (
            Authority(external_writes=True),
            "strict",
            Authority(external_writes=True),
            (),
            "STRICT_OPERATION_BOUNDARY_REQUIRED",
        ),
        (
            Authority(external_writes=True),
            "balanced",
            Authority(external_writes=True),
            (),
            "EXTERNAL_VERIFICATION_PATH_UNAVAILABLE",
        ),
        (
            Authority(external_writes=True),
            "balanced",
            Authority(external_writes=True, operation_approval=True),
            (),
            "AUTHORITY_EXCEEDS_POLICY",
        ),
    ],
)
def test_each_runtime_rejection_is_projected_and_repairable(
    authority, security, ceiling, checks, reason
):
    cfg = config().model_copy(update={"security": security, "authority_ceiling": ceiling})
    task = Task(
        id="local",
        description="work",
        criteria=(Criterion(id="result", description="done"),),
        verification_plan="inspect",
        authority=authority,
    )
    plan = Plan(tasks=(task,), result_task=task.id)
    violation = task_violation(task, cfg)
    assert violation["reason"] == reason
    with pytest.raises(Stopped, match=reason):
        readiness(task, cfg)
    for stage in ("planning", "recovery"):
        contract = StageContract.bind(stage, {}, cfg)
        with pytest.raises(OutputViolation, match=reason) as caught:
            contract.validate(plan, {}, cfg)
        assert caught.value.details == violation
        assert violation["rule"] == contract.projection()["authority"]["rules"][reason]["rule"]


def test_bad_plan_repairs_before_worker_with_same_contract_and_preserved_evidence(tmp_path):
    model = AuthorityModel()
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write result", config(), source)
    assert model.workers == 1 and len(model.prompts) == 2
    assert model.prompts[0]["stage_contract"] == model.prompts[1]["stage_contract"]
    feedback = model.prompts[1]["contract_feedback"]
    assert feedback["violation"]["task"] == "write"
    assert feedback["violation"]["unsupported_fields"] == ["duplicate_prevention"]
    events = Journal(engine.journal.path).events(run)
    rejected = next(i for i, e in enumerate(events) if e["kind"] == "output_rejected")
    adopted = next(i for i, e in enumerate(events) if e["kind"] == "plan")
    worker = next(i for i, e in enumerate(events) if e["kind"] == "attempt_started")
    assert rejected < adopted < worker
    assert events[rejected]["body"]["violation"] == feedback["violation"]
    assert [e["body"]["usage"]["tokens"] for e in events if e["kind"] == "settled"][:3] == [
        10,
        17,
        17,
    ]
    responses = [
        json.loads(e["body"]["record"]["text"])
        for e in events
        if e["kind"] == "diagnostic"
        and json.loads(e["body"]["context"]["text"]).get("kind") == "model_response"
        and e["body"]["stage"] == "planning"
    ]
    assert [r["tasks"][0]["authority"]["duplicate_prevention"] for r in responses] == [True, False]
    calls = len(model.calls)
    engine.execute(run)
    assert len(model.calls) == calls


def test_repair_exhaustion_preserves_reason_and_reopen_cannot_refund(tmp_path):
    model = AuthorityModel(always=True)
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", config(), source)
    with pytest.raises(RuntimeError, match="OUTPUT_REPAIR_EXHAUSTED"):
        engine.execute(run)
    budget = engine.journal.budget(run)
    reopened = Engine(Journal(engine.journal.path), engine.candidates, model, engine.root)
    with pytest.raises(Stopped, match="STAGE_INVOCATION_LIMIT"):
        reopened.execute(run)
    assert len(model.prompts) == 2 and model.workers == 0
    assert reopened.journal.budget(run)["measured_usage"] == budget["measured_usage"]
    assert all(
        e["body"]["reason"] == "REQUIRED_AUTHORITY_BOUNDARY_UNAVAILABLE"
        for e in reopened.journal.events(run)
        if e["kind"] == "output_rejected"
    )


def test_crash_during_repair_keeps_feedback_and_consumes_reserved_attempt(tmp_path):
    model = AuthorityModel(crash=True)
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(update={"planning": StagePolicy(model="test", revisions=2)})
    run = engine.prepare("Write result", cfg, source)
    with pytest.raises(SystemExit, match="controller lost"):
        engine.execute(run)
    reopened = Engine(Journal(engine.journal.path), engine.candidates, model, engine.root)
    reopened.execute(run)
    assert model.workers == 1
    assert model.prompts[-1]["contract_feedback"]["violation"]["task"] == "write"
    reservations = [
        e["body"]
        for e in reopened.journal.events(run)
        if e["kind"] == "reserved" and e["body"]["stage"] == "planning"
    ]
    assert len(reservations) == 3


def test_shared_budget_can_stop_repair_before_another_model_call(tmp_path):
    model = AuthorityModel()
    engine, source = runtime(tmp_path, model)
    with pytest.raises(Stopped):
        engine.start("Write result", config(attempts=2), source)
    assert len(model.prompts) == 1 and model.workers == 0


@pytest.mark.parametrize("drop_outcome", [False, True])
def test_repair_cannot_remove_required_external_boundary_or_outcome(tmp_path, drop_outcome):
    class External(AuthorityModel):
        def generate(self, policy, prompt, schema, *args, **kwargs):
            result, usage = super().generate(policy, prompt, schema, *args, **kwargs)
            payload = result.model_dump(mode="json")
            if schema.__name__ == "Clarification":
                payload["criteria"][0].update(outcome="external_effect", checks=["receipt"])
            if schema is Plan:
                task = payload["tasks"][0]
                task["criteria"][0].update(outcome="external_effect", checks=["receipt"])
                task["authority"].update(
                    external_writes=True, operation_approval=True, duplicate_prevention=True
                )
                if len(self.prompts) > 1:
                    task["authority"].update(operation_approval=False, duplicate_prevention=False)
                    if drop_outcome:
                        task["authority"]["external_writes"] = False
                        task["criteria"][0]["outcome"] = "artifact"
            return schema.model_validate(payload), usage

    model = External()
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={
            "authority_ceiling": Authority(external_writes=True),
            "checks": (Check(id="receipt", argv=("true",), evidence_kind="external_effect"),),
        }
    )
    with pytest.raises(RuntimeError, match="OUTPUT_REPAIR_EXHAUSTED"):
        engine.start("Write result", cfg, source)
    assert model.workers == 0 and len(model.prompts) == 2
    reasons = [
        e["body"]["reason"]
        for e in engine.journal.events(engine.journal.runs()[0])
        if e["kind"] == "output_rejected"
    ]
    assert reasons == [
        "REQUIRED_AUTHORITY_BOUNDARY_UNAVAILABLE",
        "EXTERNAL_GOAL_WEAKENED" if drop_outcome else "STRICT_OPERATION_BOUNDARY_REQUIRED",
    ]


def test_previous_contract_version_cannot_resume_with_fresh_authority_counters(
    tmp_path, monkeypatch
):
    import ai_employee.history as history

    journal = Journal(tmp_path / "history.db")
    with monkeypatch.context() as patch:
        patch.setattr(history, "VERSION", "stage-contract-1")
        run = journal.create("Write result", config())
    with pytest.raises(Stopped, match="CONTRACT_VERSION_CHANGED"):
        Journal(journal.path).reserve(
            run, "planning", call_key="new-authority-contract", call_limit=2
        )


def test_explicit_coarse_permission_remains_valid_and_is_not_rewritten():
    cfg = config().model_copy(
        update={
            "security": "balanced",
            "authority_ceiling": Authority(network_hosts=("*.example.com",), external_writes=True),
            "checks": (Check(id="receipt", argv=("true",), evidence_kind="external_effect"),),
        }
    )
    task = Task(
        id="publish",
        description="Publish authorized result",
        authority=Authority(network_hosts=("api.example.com",), external_writes=True),
        criteria=(
            Criterion(
                id="published",
                description="remote result",
                checks=("receipt",),
                outcome="external_effect",
            ),
        ),
        verification_plan="check receipt",
    )
    plan = Plan(tasks=(task,), result_task=task.id)
    contract = StageContract.bind("planning", {}, cfg)
    assert contract.validate(plan, {}, cfg) == plan
    assert readiness(task, cfg)["state"] == "plannable"


def test_zero_revisions_never_retries_an_invalid_authority(tmp_path):
    model = AuthorityModel()
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(update={"planning": StagePolicy(model="test", revisions=0)})
    with pytest.raises(RuntimeError, match="OUTPUT_REPAIR_EXHAUSTED"):
        engine.start("Write result", cfg, source)
    assert len(model.prompts) == 1 and model.workers == 0
