"""Semantic ownership through provider projection, repair and real Engine decisions."""

import json

import pytest
from pydantic import ValidationError

from ai_employee.cli import projection as inspect_run
from ai_employee.history import Stopped
from ai_employee.models import (
    Authority,
    Criterion,
    Finding,
    Plan,
    StagePolicy,
    Task,
    Verification,
    WorkerChoice,
    WorkerResult,
)
from ai_employee.native import decode_response, provider_schema
from ai_employee.semantics import (
    EVIDENCE,
    FINDING_CATEGORIES,
    GRAPH,
    OUTCOMES,
    RULES,
    WORKER_STATES,
)
from ai_employee.stage_contracts import OutputViolation, StageContract, repair_feedback

from .test_autonomous_runtime import OfflineModel, clarification, config, runtime
from .test_stage_contracts import stream


@pytest.mark.parametrize("state", list(WORKER_STATES))
@pytest.mark.parametrize("requested", [None, Authority()])
def test_worker_relationship_and_action_share_one_definition(state, requested):
    expected = WORKER_STATES[state]
    if expected["request"] != (requested is not None):
        with pytest.raises(ValidationError, match="WORKER_AUTHORITY_REQUEST_MISMATCH"):
            WorkerResult(status=state, summary="report", authority_request=requested)
    else:
        result = WorkerResult(status=state, summary="report", authority_request=requested)
        assert result.action(False) == expected["action"]
        assert result.action(True) == expected["external_action"]


@pytest.mark.parametrize("category", list(FINDING_CATEGORIES))
@pytest.mark.parametrize("passed", [False, True])
def test_finding_provider_and_validator_allow_the_same_combinations(category, passed):
    schema = provider_schema(Verification)
    branches = schema["$defs"]["Finding"]["anyOf"]
    allowed = any(
        passed in branch["properties"]["passed"]["enum"]
        and category in branch["properties"]["category"]["enum"]
        for branch in branches
    )
    expected = FINDING_CATEGORIES[category]["passed"]
    assert allowed == (expected is None or expected == passed)
    if allowed:
        finding = Finding(
            criterion_id="result", passed=passed, evidence="observed", category=category
        )
        assert (
            Verification(findings=(finding,), summary="review").accepts(
                (Criterion(id="result", description="actual result"),)
            )
            == passed
        )
    else:
        with pytest.raises(ValidationError, match="FINDING_CATEGORY_MISMATCH"):
            Finding(criterion_id="result", passed=passed, evidence="observed", category=category)


def test_review_provider_exact_cardinality_references_and_repair_context():
    binding = StageContract.bind("clarification_review", {"proposal": {}}, config())
    schema = provider_schema(Verification, binding.projection())
    assert schema["properties"]["findings"]["minItems"] == 1
    assert schema["properties"]["findings"]["maxItems"] == 1
    for branch in schema["$defs"]["Finding"]["anyOf"]:
        assert branch["properties"]["criterion_id"]["enum"] == ["review"]
    value = Verification(
        findings=tuple(
            Finding(criterion_id="review", passed=True, evidence="claim") for _ in range(4)
        ),
        summary="four concerns",
    )
    with pytest.raises(OutputViolation) as fault:
        binding.validate(value, {}, config())
    feedback = repair_feedback(str(fault.value), fault.value.details)
    assert feedback["expected"] == ["review"]
    assert feedback["received"] == ["review"] * 4
    assert feedback["rule"] == RULES["INVALID_FINDING_REFERENCES"]["rule"]


class SemanticModel(OfflineModel):
    def __init__(self, fault=None):
        super().__init__()
        self.fault = fault
        self.prompts = []
        self.faults = 0

    def generate(self, policy, prompt, schema, workspace, authority, timeout, cancelled, **kw):
        body = json.loads(prompt)
        self.prompts.append(body)
        stage = body["stage_contract"]["stage"]
        meanings = body["stage_contract"]["semantics"]
        assert meanings["evidence"] == EVIDENCE
        assert meanings["outcomes"] == OUTCOMES
        if stage.endswith("_review"):
            # Pre-work review knows that optional checks and future evidence differ.
            assert "future artifacts" in meanings["evidence"]["proposal_review"]
            payload = Verification(
                findings=(
                    Finding(
                        criterion_id="review", passed=True, evidence="evidence route is feasible"
                    ),
                ),
                summary="accepted",
            ).model_dump(mode="json")
            if self.fault == "review_count" and not self.faults:
                self.faults += 1
                payload["findings"] *= 4
            return decode_response(stream(payload), schema)
        result, usage = super().generate(
            policy, prompt, schema, workspace, authority, timeout, cancelled, **kw
        )
        if (
            schema is WorkerResult
            and self.fault
            in (
                "request",
                "completed",
                "usage_limit",
                "uncertain",
            )
            and not self.faults
        ):
            self.faults += 1
            payload = result.model_dump(mode="json")
            payload["status"] = "authority_requested" if self.fault == "request" else self.fault
            payload["authority_request"] = (
                None if self.fault == "request" else Authority().model_dump(mode="json")
            )
            return decode_response(stream(payload), schema)
        if schema is Verification and self.fault == "category" and not self.faults:
            self.faults += 1
            payload = result.model_dump(mode="json")
            payload["findings"][0]["category"] = "missing_evidence"
            return decode_response(stream(payload), schema)
        if schema is Plan:
            result = result.model_copy(
                update={
                    "tasks": tuple(
                        task.model_copy(
                            update={
                                "required_evidence": ("actual result.txt produced by later work",),
                            }
                        )
                        for task in result.tasks
                    )
                }
            )
        return result, usage


@pytest.mark.parametrize(
    "fault,code",
    [
        ("review_count", "INVALID_FINDING_REFERENCES"),
        ("request", "WORKER_AUTHORITY_REQUEST_MISMATCH"),
        ("completed", "WORKER_AUTHORITY_REQUEST_MISMATCH"),
        ("category", "FINDING_CATEGORY_MISMATCH"),
    ],
)
def test_shared_semantics_survive_review_repair_verification_and_replay(tmp_path, fault, code):
    model = SemanticModel(fault)
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={
            name: StagePolicy(model="test", review="always")
            for name in ("clarification", "planning", "worker", "verification")
        }
    )
    run = engine.start("Write result", cfg, source)
    assert inspect_run(engine.journal, run)["status"] == "completed"
    repairs = [p["contract_feedback"] for p in model.prompts if p["contract_feedback"]]
    repair = next(r for r in repairs if r["code"] == code)
    assert repair["violation"]["rule"] == RULES[code]["rule"]
    assert repair["violation"]["authoritative"] is False
    before = len(model.prompts)
    engine.execute(run)
    assert len(model.prompts) == before


@pytest.mark.parametrize("status", ["usage_limit", "uncertain"])
def test_malformed_safety_report_cannot_trigger_output_retry(tmp_path, status):
    model = SemanticModel(status)
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", config(), source)
    if status == "usage_limit":
        with pytest.raises(Stopped, match="USAGE_LIMIT"):
            engine.execute(run)
    else:
        engine.execute(run)
        assert inspect_run(engine.journal, run)["status"] == "uncertain"
    assert model.workers == 1
    assert engine.journal.budget(run)["open_reservations"] == 0


def test_graph_recovery_producer_reviewer_and_validation_share_context():
    old = Task(
        id="old", description="old", criteria=clarification().criteria, verification_plan="inspect"
    )
    context = {
        "historical_tasks": [old.model_dump(mode="json")],
        "original_task_ids": ["old"],
        "failed_tasks": ["old"],
    }
    cfg = config(added_tasks=1)
    binding = StageContract.bind("recovery", context, cfg)
    reviewer = StageContract.bind("recovery_review", {"original": context}, cfg)
    assert binding.constraints == reviewer.constraints
    assert binding.projection()["semantics"]["graph"] == GRAPH
    repaired = old.model_copy(update={"id": "new", "kind": "repair", "supersedes": "old"})
    plan = Plan(tasks=(repaired,), result_task="new")
    assert binding.validate(plan, context, cfg) == plan
    with pytest.raises(OutputViolation, match="GRAPH_GROWTH_LIMIT"):
        binding.validate(Plan(tasks=(old,), result_task="old"), context, cfg)
    with pytest.raises(OutputViolation, match="FOREIGN_REPAIR_TARGET"):
        binding.validate(
            Plan(tasks=(repaired.model_copy(update={"supersedes": "foreign"}),), result_task="new"),
            context,
            cfg,
        )
    with pytest.raises(OutputViolation, match="FAILED_DEPENDENCY"):
        binding.validate(
            Plan(
                tasks=(old, repaired.model_copy(update={"dependencies": ("old",)})),
                result_task="new",
            ),
            context,
            cfg,
        )


def test_selection_index_constraint_is_visible_to_producer_and_reviewer():
    context = {"options": [{}, {}]}
    producer = StageContract.bind("selection", context, config())
    reviewer = StageContract.bind("selection_review", {"original": context}, config())
    assert producer.constraints == reviewer.constraints
    assert (
        provider_schema(WorkerChoice, producer.projection())["properties"]["index"]["maximum"] == 1
    )
    with pytest.raises(OutputViolation, match="WORKER_SELECTION_OUT_OF_RANGE"):
        producer.validate(WorkerChoice(index=2, reason="invented choice"), context, config())


def test_repair_context_is_bounded_redacted_and_excludes_raw_proposals():
    result = repair_feedback(
        "INVALID_FINDING_REFERENCES",
        {
            "expected": ["review"],
            "received": ["Bearer private-canary-12345"] * 1000,
            "payload": {"summary": "private-proposal-canary"},
            "authority": {"credentials": ["private-authority-canary"]},
        },
    )
    text = json.dumps(result)
    assert len(text.encode()) < 10000
    assert "private-" not in text and "[REDACTED]" in text
    assert result["expected"] == ["review"]
    assert result["context_truncated"] is True
    assert "complete StageContract" in result["reference_source"]


def test_authority_host_schema_and_repair_share_local_contract():
    from ai_employee.models import AUTHORITY_HOST_PATTERN
    from ai_employee.stage_contracts import validation_code

    binding = StageContract.bind("planning", {}, config())
    schema = provider_schema(Plan, binding.projection())
    hosts = schema["$defs"]["Authority"]["properties"]["network_hosts"]
    assert hosts["items"]["pattern"] == AUTHORITY_HOST_PATTERN
    for hosts, code in [
        (("https://example.com",), "AUTHORITY_REQUIRES_HOST_NAMES_NOT_URLS_OR_PORTS"),
        (("example.com", "example.com"), "DUPLICATE_AUTHORITY_RESOURCE"),
    ]:
        with pytest.raises(ValidationError) as fault:
            Authority(network_hosts=hosts)
        assert validation_code(fault.value) == code
        assert repair_feedback(code, {})["rule"] == RULES[code]["rule"]
