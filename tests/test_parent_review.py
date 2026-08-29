from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from ai_employee.domain import (
    Budget,
    CompletionCriterion,
    EvaluationDecision,
    ExecutionStrategy,
    Goal,
    Graph,
    Node,
    NodeKind,
    OutputContract,
    RoutingMode,
)
from ai_employee.domain.base import freeze_json
from ai_employee.domain.browser import (
    BrowserAction,
    BrowserCapture,
    BrowserObservation,
    BrowserScenario,
)
from ai_employee.domain.evaluation import (
    CriterionOutcome,
    CriterionResult,
    EvaluationBudget,
    EvaluationEvidenceLedger,
    EvaluationFreshness,
    EvaluationRequest,
    EvaluationResult,
    EvaluatorBehavior,
    EvaluatorSpecification,
    ObservationManifest,
)
from ai_employee.domain.models import AcceptedGraphRevision
from ai_employee.domain.v2 import (
    ArtifactDescriptor,
    CriterionEvidence,
    DecisionOutcome,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
)
from ai_employee.graph_evaluation import (
    GraphCandidateEvaluator,
    ParentCandidateEvaluationRequest,
    ParentVerificationBinding,
)
from ai_employee.parent_review import (
    CliParentSemanticReviewer,
    ParentNodeReviewBinding,
    ParentSemanticBasis,
    ParentSemanticConfidence,
    ParentSemanticFinding,
    ParentSemanticFindingType,
    ParentSemanticReviewPayload,
    ParentSemanticReviewRequest,
    ParentSemanticSeverity,
    bind_parent_semantic_review_payload,
    decide_parent_semantic_review,
    parent_semantic_review_schema_json,
    parse_parent_semantic_review_payload,
    validate_parent_semantic_review_result,
)
from ai_employee.serialization import canonical_digest, canonical_json, versioned_digest
from ai_employee.storage import SQLiteStore

NOW = datetime(2026, 8, 29, tzinfo=UTC)
RUN = "parent-review-run"
HARNESS = "1" * 64
POLICY = "2" * 64


def _strategy(backend: str = "ollama_cli") -> ExecutionStrategy:
    return ExecutionStrategy(
        id=f"parent-reviewer-{backend}",
        routing_mode=RoutingMode.ADAPTIVE,
        backend=backend,
        model="reviewer-model",
        effort="low",
        capabilities=("process",),
    )


def _request(
    strategy: ExecutionStrategy | None = None,
    criterion_ids: tuple[str, ...] = ("criterion",),
) -> ParentSemanticReviewRequest:
    criteria = tuple(
        CompletionCriterion(id=item, description=f"{item} integrated behavior works")
        for item in criterion_ids
    )
    nodes = tuple(
        Node(
            id=node_id,
            kind=NodeKind.FUNCTION,
            name=node_id,
            objective=f"implement {node_id}",
            output_contract=OutputContract(id=f"contract-{node_id}"),
            completion_criteria=criteria,
        )
        for node_id in ("a", "b")
    )
    graph = Graph(
        id="graph",
        nodes=nodes,
        entry_node_ids=("a", "b"),
        terminal_node_ids=("a", "b"),
        budget=Budget(max_attempts=2, max_nodes=2, max_wall_seconds=30.0),
    )
    revision = AcceptedGraphRevision(revision_number=1, graph=graph)
    goal = Goal(
        id="goal",
        statement="integrate both tasks",
        completion_criteria=criteria,
    )
    candidate_body = b"diff --git a/a.py b/a.py\n+INTEGRATED = False\n"
    candidate = ArtifactDescriptor(
        id="candidate",
        run_id=RUN,
        created_at=NOW,
        artifact_digest=hashlib.sha256(candidate_body).hexdigest(),
        media_type="text/x-diff",
        size_bytes=len(candidate_body),
        logical_kind="workspace_patch",
        producer_action_id="composition-workspace",
        source=freeze_json({"base_tree": "7" * 64}),
        store_locator="sha256/candidate",
    )
    candidate_digest = canonical_digest({"candidate": candidate.artifact_digest})
    results = tuple(
        CriterionResult(
            criterion_id=item,
            outcome=CriterionOutcome.SATISFIED,
            explanation="deterministic tests pass",
        )
        for item in criterion_ids
    )
    ledger = EvaluationEvidenceLedger(
        id="ledger",
        run_id=RUN,
        created_at=NOW,
        candidate_digest=candidate_digest,
        generation=1,
        evaluator_specification_digest="3" * 64,
        effective_policy_digest=POLICY,
        evaluation_result_digests=("4" * 64,),
        expected_criterion_ids=criterion_ids,
        criterion_results=results,
        freshness=EvaluationFreshness(fresh=True),
        remaining_budget=EvaluationBudget(),
        behavior=EvaluatorBehavior.DETERMINISTIC,
        decision=EvaluationDecision.PASS,
    )
    evidence = tuple(
        CriterionEvidence(
            criterion_id=item,
            disposition="satisfied",
            evidence_refs=(ledger.content_digest or "0" * 64,),
        )
        for item in criterion_ids
    )
    return ParentSemanticReviewRequest(
        id="request",
        run_id=RUN,
        created_at=NOW,
        goal=goal,
        goal_digest=canonical_digest(goal),
        accepted_revision=revision,
        accepted_graph_revision_digest=revision.content_digest or "0" * 64,
        generation=1,
        reviewer_strategy=strategy or _strategy(),
        harness_digest=HARNESS,
        effective_policy_digest=POLICY,
        composition_record_digest="5" * 64,
        composition_workspace_digest="6" * 64,
        candidate_digest=candidate_digest,
        candidate_descriptor=candidate,
        candidate_descriptor_digest=candidate.content_digest or "0" * 64,
        candidate_artifact_digest=candidate.artifact_digest,
        node_bindings=tuple(
            ParentNodeReviewBinding(
                node_id=node.id,
                generation=node.generation,
                result_generation=node.generation,
                attempt=node.attempt,
                objective_digest=canonical_digest(node.objective),
                completion_criteria_digest=canonical_digest(node.completion_criteria),
                worker_request_digest=("8" if node.id == "a" else "9") * 64,
                worker_result_digest=("a" if node.id == "a" else "b") * 64,
                evidence_digest=("c" if node.id == "a" else "d") * 64,
                evaluator_digest=("e" if node.id == "a" else "f") * 64,
            )
            for node in nodes
        ),
        deterministic_ledgers=(ledger,),
        deterministic_ledger_digests=(ledger.content_digest or "0" * 64,),
        criterion_evidence=evidence,
    )


def _finding(
    request: ParentSemanticReviewRequest,
    *,
    repair_objective: str | None = "make task b consume task a's output",
    confidence: ParentSemanticConfidence = ParentSemanticConfidence.CERTAIN,
    criterion_ids: tuple[str, ...] = ("criterion",),
) -> ParentSemanticFinding:
    return ParentSemanticFinding(
        id="cross-task-gap",
        finding_type=ParentSemanticFindingType.INTEGRATION_CONSISTENCY,
        severity=ParentSemanticSeverity.HIGH,
        confidence=confidence,
        basis=ParentSemanticBasis.OBSERVED,
        criterion_ids=criterion_ids,
        node_ids=("a", "b"),
        observation="all tests pass but task b ignores task a's produced value",
        rationale="the exact candidate hard-codes the pre-integration behavior",
        evidence_digests=(request.candidate_artifact_digest,),
        artifact_digests=(request.candidate_artifact_digest,),
        repair_objective=repair_objective,
    )


def _result(
    request: ParentSemanticReviewRequest,
    finding: ParentSemanticFinding,
):
    return bind_parent_semantic_review_payload(
        ParentSemanticReviewPayload(
            findings=(finding,),
            reviewed_criterion_ids=request.criterion_ids,
            reviewed_node_ids=("a", "b"),
        ),
        request=request,
        record_id="result",
        run_id=RUN,
        created_at=NOW,
    )


def test_wire_order_is_canonicalized_but_duplicates_fail_closed() -> None:
    request = _request()
    finding = _finding(request)
    raw = json.loads(
        canonical_json(
            ParentSemanticReviewPayload(
                findings=(finding,),
                reviewed_criterion_ids=("criterion",),
                reviewed_node_ids=("a", "b"),
                limitations=("first limitation", "second limitation"),
            )
        )
    )
    raw["reviewed_node_ids"] = ["b", "a"]
    raw["findings"][0]["node_ids"] = ["b", "a"]
    raw["limitations"] = ["second limitation", "first limitation"]

    parsed = parse_parent_semantic_review_payload(json.dumps(raw))

    assert parsed.reviewed_node_ids == ("a", "b")
    assert parsed.findings[0].node_ids == ("a", "b")
    assert parsed.limitations == ("first limitation", "second limitation")
    raw["reviewed_node_ids"] = ["a", "a"]
    with pytest.raises(ValueError, match="unique"):
        parse_parent_semantic_review_payload(json.dumps(raw))


def test_cross_task_gap_maps_to_repair_only_with_bounded_objective() -> None:
    request = _request()
    repairable = _result(request, _finding(request))
    decision = decide_parent_semantic_review(
        request,
        repairable,
        block_severities=(ParentSemanticSeverity.HIGH,),
        decision_id="decision",
        run_id=RUN,
        created_at=NOW,
    )
    assert decision.action is EvaluationDecision.REPAIR

    unrecoverable = _result(request, _finding(request, repair_objective=None))
    assert (
        decide_parent_semantic_review(
            request,
            unrecoverable,
            block_severities=(ParentSemanticSeverity.HIGH,),
            decision_id="unrecoverable-decision",
            run_id=RUN,
            created_at=NOW,
        ).action
        is EvaluationDecision.FAIL
    )

    uncertain = _result(
        request,
        _finding(request, confidence=ParentSemanticConfidence.UNCERTAIN),
    )
    assert (
        decide_parent_semantic_review(
            request,
            uncertain,
            block_severities=(ParentSemanticSeverity.HIGH,),
            decision_id="uncertain-decision",
            run_id=RUN,
            created_at=NOW,
        ).action
        is EvaluationDecision.ESCALATE
    )


def test_finding_digest_is_merged_only_into_its_affected_criterion() -> None:
    request = _request(criterion_ids=("criterion-a", "criterion-b"))
    finding = _finding(request, criterion_ids=("criterion-a",))
    result = _result(request, finding)
    decision = decide_parent_semantic_review(
        request,
        result,
        block_severities=(ParentSemanticSeverity.HIGH,),
        decision_id="decision",
        run_id=RUN,
        created_at=NOW,
    )
    chain = tuple(
        item
        for item in (request.content_digest, result.content_digest, decision.content_digest)
        if item is not None
    )
    criterion_evidence = {item.criterion_id: item for item in request.criterion_evidence}
    evaluator = object.__new__(GraphCandidateEvaluator)

    evaluator._merge_semantic_evidence(
        criterion_evidence,
        request,
        result,
        decision,
        chain,
    )

    finding_digest = canonical_digest(finding)
    assert finding_digest in criterion_evidence["criterion-a"].evidence_refs
    assert finding_digest not in criterion_evidence["criterion-b"].evidence_refs
    assert set(chain) <= set(criterion_evidence["criterion-a"].evidence_refs)
    assert set(chain) <= set(criterion_evidence["criterion-b"].evidence_refs)
    assert criterion_evidence["criterion-a"].disposition == "blocked"
    assert criterion_evidence["criterion-b"].disposition == "satisfied"


def test_parent_semantic_result_rejects_foreign_candidate() -> None:
    request = _request()
    result = _result(request, _finding(request)).model_copy(update={"candidate_digest": "f" * 64})
    with pytest.raises(ValueError, match="stale or foreign"):
        validate_parent_semantic_review_result(request, result)


def test_resume_deduplicates_exact_content_but_rejects_changed_evidence(
    tmp_path: Path,
) -> None:
    request = _request()
    ledger = request.deterministic_ledgers[0]
    ledger_changed = EvaluationEvidenceLedger.model_validate_json(
        canonical_json(
            ledger.model_copy(
                update={
                    "id": "changed-ledger",
                    "content_digest": None,
                    "evaluation_result_digests": ("e" * 64,),
                }
            )
        ),
        strict=True,
    )
    changed_evidence = CriterionEvidence(
        criterion_id="criterion",
        disposition="satisfied",
        evidence_refs=(ledger_changed.content_digest or "0" * 64,),
    )
    changed = ParentSemanticReviewRequest.model_validate_json(
        canonical_json(
            request.model_copy(
                update={
                    "id": "changed-request",
                    "content_digest": None,
                    "deterministic_ledgers": (ledger_changed,),
                    "deterministic_ledger_digests": (ledger_changed.content_digest or "0" * 64,),
                    "criterion_evidence": (changed_evidence,),
                }
            )
        ),
        strict=True,
    )
    with SQLiteStore(tmp_path / "resume.db") as store:
        store.put("evaluation_evidence_ledger_v2", ledger, run_id=RUN)
        store.put("parent_semantic_review_request_v2", request, run_id=RUN)
        # A metadata-only duplicate has the same validated content identity.
        duplicate = request.model_copy(update={"id": "duplicate-request"})
        store.put("parent_semantic_review_request_v2", duplicate, run_id=RUN)
        evaluator = object.__new__(GraphCandidateEvaluator)
        evaluator.store = store

        assert evaluator._resumable_semantic_request(request).content_digest == (
            request.content_digest
        )
        assert evaluator._resumable_semantic_request(changed) is None


@pytest.mark.parametrize("provider_id", ["process.harness", "browser.playwright"])
@pytest.mark.parametrize("runtime_state", ["missing", "foreign"])
def test_resumed_semantic_evidence_requires_exact_runtime_result(
    tmp_path: Path,
    provider_id: str,
    runtime_state: str,
) -> None:
    request = _request()
    process_request = (
        ProcessRequest(
            id="runtime-process",
            run_id=RUN,
            created_at=NOW,
            argv=("verify",),
            purpose="test exact runtime binding",
        )
        if provider_id == "process.harness"
        else None
    )
    scenario = (
        BrowserScenario(
            origin="http://127.0.0.1:3000",
            actions=(BrowserAction(kind="navigate", url="http://127.0.0.1:3000/"),),
            captures=(
                BrowserCapture(
                    id="runtime-screen",
                    kind="screenshot",
                    logical_kind="browser_screenshot",
                ),
            ),
        )
        if provider_id == "browser.playwright"
        else None
    )
    specification = EvaluatorSpecification(
        id="runtime-specification",
        run_id=RUN,
        created_at=NOW,
        provider_id=provider_id,
        provider_schema_version="v2",
        provider_descriptor_digest="3" * 64,
        behavior=EvaluatorBehavior.DETERMINISTIC,
        required_capabilities=("process" if process_request is not None else "browser",),
        command_ref=None if process_request is None else process_request.id,
        browser_scenario=scenario,
        criterion_ids=("criterion",),
    )
    binding = ParentVerificationBinding(
        harness_evaluator_id="runtime-evaluator",
        harness_command_ref=None if process_request is None else "verify",
        specification=specification,
        process_request=process_request,
    )
    evaluation_request = EvaluationRequest(
        id="runtime-evaluation-request",
        run_id=RUN,
        created_at=NOW,
        candidate_digest=request.candidate_digest,
        generation=request.generation,
        evaluator_specification_digest=specification.content_digest or "0" * 64,
        effective_policy_digest=POLICY,
    )
    evaluation_request_digest = evaluation_request.content_digest or "0" * 64
    if runtime_state == "foreign" and process_request is not None:
        runtime: ExecutionResult | BrowserObservation | None = ExecutionResult(
            id="foreign-runtime-process",
            run_id=RUN,
            created_at=NOW,
            request_digest="f" * 64,
            status="succeeded",
            exit_code=0,
            duration_seconds=0.01,
        )
    elif runtime_state == "foreign":
        assert scenario is not None
        runtime = BrowserObservation(
            id="foreign-runtime-browser",
            run_id=RUN,
            created_at=NOW,
            request_digest="f" * 64,
            scenario_digest=versioned_digest(scenario),
            session_id="foreign-session",
            status="succeeded",
            final_url=scenario.origin,
            actions_completed=len(scenario.actions),
            duration_seconds=0.01,
        )
    else:
        runtime = None
    manifest = ObservationManifest(
        id="runtime-manifest",
        run_id=RUN,
        created_at=NOW,
        request_digest=evaluation_request_digest,
        candidate_digest=request.candidate_digest,
        generation=request.generation,
        evaluator_specification_digest=specification.content_digest or "0" * 64,
        effective_policy_digest=POLICY,
    )
    evaluation_result = EvaluationResult(
        id="runtime-evaluation-result",
        run_id=RUN,
        created_at=NOW,
        request_digest=evaluation_request_digest,
        candidate_digest=request.candidate_digest,
        generation=request.generation,
        evaluator_specification_digest=specification.content_digest or "0" * 64,
        effective_policy_digest=POLICY,
        provider_descriptor_digest=specification.provider_descriptor_digest,
        behavior=EvaluatorBehavior.DETERMINISTIC,
        expected_criterion_ids=("criterion",),
        observation_manifest=manifest,
        execution_result_digest=(
            "5" * 64 if runtime is None else runtime.content_digest or "0" * 64
        ),
        criterion_results=(
            CriterionResult(
                criterion_id="criterion",
                outcome=CriterionOutcome.SATISFIED,
                explanation="runtime passed",
            ),
        ),
    )
    ledger = EvaluationEvidenceLedger(
        id="runtime-ledger",
        run_id=RUN,
        created_at=NOW,
        candidate_digest=request.candidate_digest,
        generation=request.generation,
        evaluator_specification_digest=specification.content_digest or "0" * 64,
        effective_policy_digest=POLICY,
        evaluation_result_digests=(evaluation_result.content_digest or "0" * 64,),
        observation_manifest_digests=(manifest.content_digest or "0" * 64,),
        expected_criterion_ids=("criterion",),
        criterion_results=evaluation_result.criterion_results,
        freshness=EvaluationFreshness(fresh=True),
        remaining_budget=EvaluationBudget(),
        behavior=EvaluatorBehavior.DETERMINISTIC,
        decision=EvaluationDecision.PASS,
    )
    semantic_request = request.model_copy(update={"deterministic_ledgers": (ledger,)})
    parent_request = cast(
        ParentCandidateEvaluationRequest,
        SimpleNamespace(verification_bindings=(binding,)),
    )
    with SQLiteStore(tmp_path / f"missing-{provider_id}.db") as store:
        store.put("evaluation_request_v2", evaluation_request, run_id=RUN)
        store.put("evaluation_result_v2", evaluation_result, run_id=RUN)
        store.put("observation_manifest_v2", manifest, run_id=RUN)
        if isinstance(runtime, ExecutionResult):
            store.put("verification_result_v2", runtime, run_id=RUN)
        elif isinstance(runtime, BrowserObservation):
            store.put("browser_observation_v2", runtime, run_id=RUN)
        evaluator = object.__new__(GraphCandidateEvaluator)
        evaluator.store = store

        with pytest.raises(ValueError, match="runtime evidence is missing"):
            evaluator._semantic_verification_results(semantic_request, parent_request)


class _Executor:
    def __init__(self) -> None:
        self.requests: list[ProcessRequest] = []

    def execute(
        self,
        request: ProcessRequest,
        _decision: PolicyDecision,
        _cancellation: object,
    ) -> ExecutionResult:
        self.requests.append(request)
        return ExecutionResult(
            id="execution",
            run_id=RUN,
            created_at=NOW,
            request_digest=request.content_digest or "0" * 64,
            status="succeeded",
            duration_seconds=0.01,
            stdout_artifact_digest="9" * 64,
        )


def _allow(request: ProcessRequest) -> PolicyDecision:
    return PolicyDecision(
        id="policy",
        run_id=RUN,
        created_at=NOW,
        request_digest=request.content_digest or "0" * 64,
        effective_policy_digest=POLICY,
        outcome=DecisionOutcome.ALLOW,
        reason_code="explicit_parent_semantic_review",
    )


def test_parent_observer_receives_only_exact_patch_and_body_free_other_descriptors() -> None:
    request = _request()
    canary = "SECRET-CANARY-MUST-NOT-EGRESS"
    other = ArtifactDescriptor(
        id="deterministic-output",
        run_id=RUN,
        created_at=NOW,
        artifact_digest="7" * 64,
        media_type="text/plain",
        size_bytes=20,
        logical_kind="process_stdout",
        producer_action_id="verification",
        source=freeze_json({"secret": canary}),
        store_locator=f"private/{canary}",
    )
    request = ParentSemanticReviewRequest.model_validate_json(
        canonical_json(
            request.model_copy(
                update={
                    "content_digest": None,
                    "artifact_descriptors": (other,),
                }
            )
        ),
        strict=True,
    )
    candidate = b"diff --git a/a.py b/a.py\n+INTEGRATED = False\n"
    output = canonical_json(
        ParentSemanticReviewPayload(
            findings=(),
            reviewed_criterion_ids=("criterion",),
            reviewed_node_ids=("a", "b"),
        )
    ).encode()
    prompts: list[bytes] = []
    candidate_reads: list[ArtifactDescriptor] = []

    def read_candidate(descriptor: ArtifactDescriptor) -> bytes:
        candidate_reads.append(descriptor)
        return candidate if descriptor == request.candidate_descriptor else b"foreign"

    reviewer = CliParentSemanticReviewer(
        _Executor(),
        lambda digest: output if digest == "9" * 64 else b"",
        read_candidate,
        _allow,
        run_id=RUN,
        strategy=request.reviewer_strategy,
        executable="ollama",
        cwd=".",
        prompt_writer=lambda value: (prompts.append(value), "8" * 64)[1],
    )

    reviewer.review(request)

    assert candidate_reads == [request.candidate_descriptor]
    prompt = json.loads(prompts[0])
    assert prompt["protocol"] == "fleet-parent-semantic-review/2"
    assert prompt["request"]["candidate_patch"] == candidate.decode()
    for descriptor in prompt["request"]["artifact_descriptors"]:
        assert set(descriptor) == {
            "artifact_digest",
            "media_type",
            "size_bytes",
            "logical_kind",
            "producer_action_id",
            "redaction_state",
        }
    assert canary not in prompts[0].decode()
    assert prompt["response_schema"] == json.loads(parent_semantic_review_schema_json())


def test_parent_observer_disables_tools_and_sessions() -> None:
    request = _request(_strategy("codex_cli"))
    reviewer = CliParentSemanticReviewer(
        _Executor(),
        lambda _digest: b"",
        lambda _descriptor: b"",
        _allow,
        run_id=RUN,
        strategy=request.reviewer_strategy,
        executable="codex",
        cwd=".",
        prompt_writer=lambda _value: "8" * 64,
        output_schema_path="parent-review.json",
    )
    argv = reviewer._argv()
    assert "--ephemeral" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--ask-for-approval") + 1] == "never"
    assert "--ignore-rules" in argv
    assert tuple(argv[index + 1] for index, item in enumerate(argv) if item == "--disable") == (
        "shell_tool",
        "unified_exec",
    )


def test_parent_observer_rejects_foreign_run_before_reading_stdout() -> None:
    request = _request()
    candidate = b"diff --git a/a.py b/a.py\n+INTEGRATED = False\n"
    stdout_reads: list[str] = []

    class ForeignRunExecutor:
        def execute(
            self,
            process_request: ProcessRequest,
            _decision: PolicyDecision,
            _cancellation: object,
        ) -> ExecutionResult:
            return ExecutionResult(
                id="foreign-execution",
                run_id="foreign-run",
                created_at=NOW,
                request_digest=process_request.content_digest or "0" * 64,
                status="succeeded",
                duration_seconds=0.01,
                stdout_artifact_digest="f" * 64,
            )

    reviewer = CliParentSemanticReviewer(
        ForeignRunExecutor(),
        lambda digest: (stdout_reads.append(digest), b"foreign body")[1],
        lambda _descriptor: candidate,
        _allow,
        run_id=RUN,
        strategy=request.reviewer_strategy,
        executable="ollama",
        cwd=".",
        prompt_writer=lambda _value: "8" * 64,
    )

    with pytest.raises(ValueError, match="invocation failed"):
        reviewer.review(request)

    assert stdout_reads == []
