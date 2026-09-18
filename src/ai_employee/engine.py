"""Goal clarification, graph scheduling, autonomous execution and independent verification."""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import ValidationError

from .candidates import Candidates
from .capabilities import (
    authority_policy_failure,
    policies,
    readiness,
    task_violation,
    validate_policy,
)
from .diagnostics import CheckOutput, attach_failure, failure_snapshots
from .history import Journal, Stopped
from .models import (
    Authority,
    Candidate,
    Clarification,
    Contract,
    Criterion,
    Goal,
    Plan,
    RunConfig,
    StagePolicy,
    Task,
    TaskContext,
    Usage,
    Verification,
    WorkerChoice,
    WorkerResult,
)
from .native import Model, ModelAtCapacity
from .semantics import EVIDENCE, GRAPH, LIFECYCLE, WORKER_STATES, external_evidence
from .stage_contracts import (
    VERSION,
    OutputViolation,
    StageContract,
    digest,
    repair_feedback,
    validation_code,
    validation_details,
)
from .time_budget import exhausted, minimum

T = TypeVar("T", bound=Contract)
_NO_AUTHORITY = Authority()


class Waiting(RuntimeError):
    """Durable user input boundary, not a failure eligible for automatic retry."""


class DirectFallback(Exception):
    """A bounded local attempt needs normal planning; never a Run-level stop."""


class Engine:
    def __init__(self, journal: Journal, candidates: Candidates, model: Model, root: Path) -> None:
        self.journal = journal
        self.candidates = candidates
        self.model = model
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _failure_diagnostic(
        self,
        run: str,
        stage: str,
        reservation: str,
        boundary: BaseException | None,
        context: dict[str, Any] | None = None,
    ) -> None:
        error = sys.exception()
        if error is None:
            return
        # Unavailable diagnostic storage cannot replace the active control exception.
        with suppress(Exception):
            self.journal.diagnostic(
                run,
                stage,
                failure_snapshots(error, boundary=boundary),
                reservation=reservation,
                kind="execution_failure",
                **(context or {}),
            )

    def _retain_partial(
        self,
        run: str,
        reservation: str,
        workspace: Path,
        error: BaseException,
        boundary: BaseException | None,
        context: dict[str, Any],
        extracted_tree: str | None = None,
    ) -> None:
        """Persist a diagnostic tree, without constructing a Candidate or lineage."""
        snapshots = failure_snapshots(error, boundary=boundary)["failures"]
        execution: dict[str, Any] = next((f["execution"] for f in snapshots if f["execution"]), {})
        termination = execution.get("termination", {})
        extraction = execution.get("partial_workspace", {})
        record: dict[str, Any] = {
            "run_id": run,
            "stage": "worker",
            "reservation": reservation,
            **context,
            "verified": False,
            "status": "unavailable",
            "reason": extraction.get("reason")
            or termination.get("retention", {}).get("reason", "termination_unavailable"),
            "failure": termination.get("reason", type(error).__name__),
            "environment": termination.get("environment"),
        }
        # A prior baseline or probe must never become a recovered workspace.
        ready = extraction.get("status") == "ready" or (
            not extraction and execution.get("workspace_returned") is True
        )
        if (
            ready
            and termination.get("phase") == "model"
            and termination.get("process_stop") == "confirmed"
        ):
            try:
                if extraction.get("status") == "ready" and extraction.get("reason") == "extracted":
                    if extracted_tree is None:
                        raise ValueError("PARTIAL_SNAPSHOT_UNAVAILABLE")
                    tree = extracted_tree
                else:
                    tree = self.candidates.capture(workspace)
                record.update(status="saved", reason="captured", tree=tree)
            except Exception as capture_error:
                record.update(status="capture_failed", reason=type(capture_error).__name__)
        elif extraction.get("status") == "capture_failed":
            record["status"] = "capture_failed"
        if record["status"] == "unavailable" and record["reason"] == "eligible":
            record["reason"] = "workspace_not_returned"
        execution["partial_artifact"] = record
        try:
            self.journal.append(run, "partial_artifact", **record)
        except Exception:
            record.update(status="capture_failed", reason="artifact_record_unavailable")
            record.pop("tree", None)
            raise

    def partials(self, run: str) -> list[dict[str, Any]]:
        return [e["body"] for e in self.journal.events(run) if e["kind"] == "partial_artifact"]

    def export_partial(self, run: str, reservation: str, destination: Path) -> dict[str, Any]:
        record = next((p for p in self.partials(run) if p["reservation"] == reservation), None)
        if record is None or record["status"] != "saved":
            raise ValueError("PARTIAL_ARTIFACT_UNAVAILABLE")
        self.candidates.export_tree(record["tree"], destination)
        return {**record, "destination": str(destination.resolve())}

    def _workspace(self, run: str, label: str, tree: str | None = None) -> Path:
        path = self.root / run / (label + "-" + uuid4().hex)
        path.mkdir(parents=True)
        if tree is not None:
            self.candidates.materialize(tree, path)
        return path

    def _cancelled(self, run: str) -> bool:
        try:
            self.journal.check(run)
        except Stopped:
            return True
        return False

    def _generate(
        self,
        run: str,
        stage: str,
        policy: StagePolicy,
        prompt: dict[str, Any],
        schema: type[T],
        workspace: Path,
        authority: Authority = _NO_AUTHORITY,
    ) -> T:
        config = self.journal.config(run)
        # Non-worker stages may inspect/tool freely in a disposable copy, but
        # neither a malformed reviewer nor a rejected planner may mutate the
        # evidence seen by the next invocation.
        input_tree = None if stage == "worker" else self.candidates.capture(workspace)
        if input_tree is not None:
            prompt = {**prompt, "input_tree": input_tree}
            if stage in {"clarification", "clarification_review"}:
                manifest = self.candidates.manifest(input_tree)
                files: list[str] = []
                size = 0
                for name in sorted(manifest):
                    if len(files) >= 64 or size + len(name) > 4096:
                        break
                    files.append(name)
                    size += len(name)
                prompt["input_snapshot"] = {
                    "tree": input_tree,
                    "file_count": len(manifest),
                    "files": files,
                    "truncated": len(files) < len(manifest),
                    "meaning": "Runtime snapshot inventory, not proof of content inspection. "
                    "Paths are relative to the actual execution workspace. Inspect these "
                    "files before declaring inputs unavailable; original-request paths may "
                    "describe another environment. A truncated list omits valid paths; "
                    "inspect the workspace. Never seek unrelated host files.",
                }
        contract = StageContract.bind(
            stage, prompt, config, original_input=self.journal.original(run)
        )
        feedback = None
        previous_faults = [
            e["body"]
            for e in self.journal.events(run)
            if e["kind"] == "output_rejected" and e["body"]["contract"] == contract.identity
        ]
        if previous_faults:
            feedback = {
                "code": previous_faults[-1]["reason"],
                "instruction": "Repair against the unchanged stage contract.",
                "violation": previous_faults[-1].get("violation"),
            }
        action = "output"
        failures = [
            e
            for e in self.journal.events(run)
            if e["kind"] in {"transport_failed", "output_rejected"}
            and e["body"].get("contract") == contract.identity
        ]
        if failures and failures[-1]["kind"] == "transport_failed":
            self._transport_retry(run, stage, policy, contract, authority, failures[-1])
            action = "transport"
        # Counts are reserved durably before invocation. A new controller cannot
        # reset the stage-local limit, including after an uncertain process launch.
        for _ in range(policy.revisions + policy.transport_retries + 1):
            try:
                return self._invoke(
                    run,
                    stage,
                    policy,
                    {
                        **prompt,
                        "stage_contract": contract.projection(),
                        "contract_feedback": feedback,
                    },
                    schema,
                    workspace if input_tree is None else self._workspace(run, stage, input_tree),
                    authority,
                    contract,
                    action,
                )
            except (TimeoutError, ConnectionError) as error:
                self.journal.append(
                    run,
                    "transport_failed",
                    stage=stage,
                    contract=contract.identity,
                    reason="MODEL_AT_CAPACITY"
                    if isinstance(error, ModelAtCapacity)
                    else type(error).__name__,
                )
                failure = next(
                    e
                    for e in reversed(self.journal.events(run))
                    if e["kind"] == "transport_failed"
                    and e["body"].get("contract") == contract.identity
                )
                self._transport_retry(run, stage, policy, contract, authority, failure)
                action = "transport"
            except OutputViolation as error:
                violation = repair_feedback(str(error), error.details)
                entries: list[tuple[str, dict[str, Any]]] = [
                    (
                        "output_rejected",
                        {
                            "stage": stage,
                            "contract": contract.identity,
                            "target": contract.target_digest,
                            "reason": str(error),
                            "authoritative": False,
                            "violation": violation,
                        },
                    )
                ]
                # Negative safety reports remain stops even if another field is malformed.
                payload = error.details.get("payload") if isinstance(error.details, dict) else None
                reported = payload.get("status") if isinstance(payload, dict) else None
                disposition = (
                    WORKER_STATES.get(reported, {}).get("action")
                    if schema is WorkerResult and isinstance(reported, str)
                    else None
                )
                if disposition == "stop":
                    entries.append(("stopped", {"reason": "USAGE_LIMIT"}))
                elif disposition == "uncertain" or authority.external_writes:
                    entries.append(
                        (
                            "uncertain",
                            {
                                "reason": "INVALID_UNCERTAIN_RESPONSE"
                                if disposition == "uncertain"
                                else "INVALID_EXTERNAL_RESPONSE",
                                "contract": contract.identity,
                            },
                        )
                    )
                # A crash must not leave a durable rejection that resumes as a repair
                # after the same response reported quota exhaustion or uncertain effects.
                self.journal.append_many(run, tuple(entries))
                if disposition == "stop":
                    raise Stopped("USAGE_LIMIT") from error
                if disposition == "uncertain" or authority.external_writes:
                    raise Waiting("UNCERTAIN_EXTERNAL_EFFECT") from error
                feedback = {
                    "code": str(error),
                    "violation": violation,
                    "instruction": "Repair the response against the same contract. "
                    "Do not change accepted "
                    "criteria, policy or evaluation target. Inspect supplied files when needed.",
                }
                action = "output"
        if stage.endswith("_review"):
            raise RuntimeError("REVIEW_UNAVAILABLE")
        raise RuntimeError("OUTPUT_REPAIR_EXHAUSTED")

    def _transport_retry(
        self,
        run: str,
        stage: str,
        policy: StagePolicy,
        contract: StageContract,
        authority: Authority,
        failure: dict[str, Any],
    ) -> None:
        if authority.external_writes:
            self.journal.append(run, "uncertain", reason="INTERRUPTED_EXTERNAL_RESPONSE")
            self.journal.check(run)
            raise Waiting("UNCERTAIN_EXTERNAL_EFFECT")
        self.journal.check(run)
        used = sum(
            e["kind"] == "reserved"
            and e["body"].get("call_key") == contract.identity + ":transport"
            for e in self.journal.events(run)
        )
        capacity = failure["body"].get("reason") == "MODEL_AT_CAPACITY"
        if used >= policy.transport_retries:
            raise RuntimeError(
                "MODEL_AT_CAPACITY_RETRIES_EXHAUSTED" if capacity else "TRANSPORT_RETRY_EXHAUSTED"
            )
        if capacity:
            # Absolute target preserves the remaining delay across controller restart.
            delay = min(5 * 2**used, 60)
            until = failure["at"] + delay
            self.journal.append(
                run,
                "transport_retry_wait",
                stage=stage,
                contract=contract.identity,
                reason="MODEL_AT_CAPACITY",
                retry=used + 1,
                not_before=until,
            )
            while time.time() < until:
                self.journal.check(run)
                time.sleep(min(0.1, max(0.0, until - time.time())))
            self.journal.check(run)

    def _invoke(
        self,
        run: str,
        stage: str,
        policy: StagePolicy,
        prompt: dict[str, Any],
        schema: type[T],
        workspace: Path,
        authority: Authority,
        contract: StageContract,
        action: str,
    ) -> T:
        config = self.journal.config(run)
        capture_policy = config.command_capture
        direct = stage == "worker" and self._direct_task(
            run, prompt.get("context", {}).get("task", {}).get("id")
        )
        direct_seconds = self._direct_remaining(run, config) if direct else None
        capture_failed = False
        task_context = prompt.get("context", prompt)
        task = task_context.get("task") if isinstance(task_context, dict) else None
        command_context = {
            "task": task.get("id") if isinstance(task, dict) else None,
            "attempt": (
                task_context.get("attempt_id")
                or (task_context.get("candidate") or {}).get("attempt_id")
            )
            if isinstance(task_context, dict)
            else None,
        }

        try:
            reservation, timeout = self.journal.reserve(
                run,
                stage,
                binding=contract.projection(),
                policy=policy,
                call_key=contract.identity + ":" + action,
                seconds_limit=direct_seconds,
                call_limit=policy.revisions + 1 if action == "output" else policy.transport_retries,
            )
        except Stopped as error:
            self.journal.stop(run, str(error))
            raise
        started = time.monotonic()
        usage = Usage(tokens=0, cost=0)
        response_payload: dict[str, Any] | None = None
        invocation_returned = False
        diagnostic_boundary = sys.exception()
        returned_execution: dict[str, Any] | None = None
        extracted_tree: str | None = None

        def observe(body: dict[str, Any]) -> None:
            nonlocal usage, capture_failed, returned_execution, extracted_tree
            if body.get("event") == "partial_workspace":
                if stage != "worker":
                    raise ValueError("PARTIAL_WORKER_REQUIRED")
                extracted_tree = self.candidates.capture(Path(body["workspace"]))
                return
            if body.get("event") == "execution_termination":
                returned_execution = body["snapshot"]
                return
            if body.get("event") in {"command_snapshot", "command_capture_failed"}:
                if capture_policy is None or not capture_policy.enabled:
                    return
                try:
                    if body["event"] == "command_capture_failed":
                        raise ValueError("COMMAND_CAPTURE_UNAVAILABLE")
                    self.journal.command_snapshot(
                        run, reservation, stage, body, capture_policy, command_context
                    )
                except Exception:
                    if not capture_failed:
                        capture_failed = True
                        with suppress(Exception):
                            self.journal.append(
                                run,
                                "command_capture_failed",
                                stage=stage,
                                reservation=reservation,
                                reason="diagnostic_capture_unavailable",
                            )
                return

            if body.get("event") == "usage_observed":
                usage = Usage.model_validate(
                    {key: body[key] for key in Usage.model_fields if key in body}
                )
            if body.get("event") == "usage_limit":
                self.journal.stop(run, "USAGE_LIMIT")
            self.journal.append(
                run, "worker_observation", reservation=reservation, stage=stage, observation=body
            )
            if direct and body.get("event") == "usage_observed":
                self._direct_remaining(run, config, usage.tokens or 0)

        try:
            validate_policy(policy)
            try:
                preflight = self.model.preflight(
                    policy,
                    workspace,
                    authority,
                    timeout,
                    lambda: self._cancelled(run),
                    checks=self.journal.config(run).checks,
                )
            except (TimeoutError, ConnectionError):
                raise
            except (ValueError, OSError) as error:
                self.journal.append(
                    run,
                    "preflight_failed",
                    stage=stage,
                    reason="ENVIRONMENT_UNAVAILABLE",
                    error_type=type(error).__name__,
                    contract=contract.identity,
                )
                raise Stopped("ENVIRONMENT_UNAVAILABLE") from error
            self.journal.append(
                run,
                "preflight",
                stage=stage,
                contract=contract.identity,
                policy_digest=contract.policy_digest,
                result=preflight,
            )
            self.journal.check(run)
            timeout = minimum(
                None if timeout is None else timeout - (time.monotonic() - started),
                self.journal.remaining_wall(run),
            )
            if exhausted(timeout):
                self.journal.check(run)
                raise TimeoutError("PREFLIGHT_TIMEOUT")
            observe({"event": "execution_budget", "phase": "after_preflight", "seconds": timeout})
            usage = Usage()  # Once launched, absent provider measurements stay unknown.
            result, returned_usage = self.model.generate(
                policy,
                json.dumps(
                    {
                        **prompt,
                        "execution_budget": {
                            "reserved_active_seconds": timeout,
                            "limits": self.journal.config(run).limits.model_dump(mode="json"),
                        },
                    },
                    ensure_ascii=False,
                ),
                schema,
                workspace,
                authority,
                timeout,
                lambda: self._cancelled(run),
                observer=lambda seconds, output_bytes: self.journal.append(
                    run,
                    "supervision",
                    reservation=reservation,
                    elapsed_seconds=seconds,
                    output_bytes=output_bytes,
                    decision="continue_within_hard_budget",
                ),
                observation=observe,
            )
            usage = returned_usage.prefer(usage)
            if direct:
                if isinstance(result, WorkerResult) and result.status == "usage_limit":
                    raise Stopped("USAGE_LIMIT")
                if isinstance(result, WorkerResult) and result.status == "uncertain":
                    self.journal.append(run, "uncertain", reason="DIRECT_REPORTED_UNCERTAIN")
                    raise Waiting("UNCERTAIN_EXTERNAL_EFFECT")
                if usage.tokens is None:
                    raise DirectFallback("direct_usage_unknown")
                self._direct_remaining(run, config, usage.tokens)
                if direct_seconds is not None and time.monotonic() - started >= direct_seconds:
                    raise DirectFallback("direct_time_budget")
            response_payload = result.model_dump(mode="json")
            self.journal.diagnostic(
                run,
                stage,
                response_payload,
                reservation=reservation,
                contract=contract.identity,
                result_digest=result.digest,
                target=contract.target_digest,
                kind="model_response",
            )
            # Fixtures and alternate adapters must pass the same validation as
            # native decoding; typed objects alone are not acceptance evidence.
            result = schema.model_validate(result.model_dump(mode="json"))
            result = contract.validate(result, prompt, self.journal.config(run))
            self.journal.check(run)
            self.journal.append(
                run,
                "stage_result",
                contract=contract.projection(),
                stage=stage,
                reservation=reservation,
                result_digest=result.digest,
                backend=policy.backend,
                model=policy.model,
                effort=policy.effort,
            )
            invocation_returned = True
            return result
        except OutputViolation as error:
            usage = error.usage.prefer(usage)
            self.journal.diagnostic(
                run,
                stage,
                error.details,
                reservation=reservation,
                contract=contract.identity,
                target=contract.target_digest,
                kind="output_rejected",
                reason=str(error),
            )
            raise
        except ValidationError as error:
            details = validation_details(error, response_payload, schema)
            self.journal.diagnostic(
                run,
                stage,
                details,
                reservation=reservation,
                contract=contract.identity,
                target=contract.target_digest,
                kind="output_rejected",
                reason=validation_code(error),
            )
            raise OutputViolation(validation_code(error), usage, details=details) from None
        except (TimeoutError, ConnectionError) as error:
            if direct and isinstance(error, TimeoutError):
                self.journal.check(run)
                raise DirectFallback("direct_timeout") from error
            raise
        except (ValueError, OSError) as error:
            # Arbitrary adapter/environment/invariant errors are not model output
            # violations. In particular, never send them into Worker graph repair.
            self.journal.append(
                run,
                "stage_failed",
                stage=stage,
                reason="ENVIRONMENT_OR_INVARIANT_FAILURE",
                error_type=type(error).__name__,
                contract=contract.identity,
            )
            raise RuntimeError("ENVIRONMENT_OR_INVARIANT_FAILURE") from error
        except Stopped as error:
            self.journal.stop(run, str(error))
            raise
        except BaseException as error:
            if any(
                event["kind"] == "stopped" and event["body"]["reason"] == "USAGE_LIMIT"
                for event in self.journal.events(run)
            ):
                raise Stopped("USAGE_LIMIT; ENVIRONMENT_CLEANUP_UNCONFIRMED") from error
            if authority.external_writes:
                self.journal.append(
                    run,
                    "uncertain",
                    reason="EXTERNAL_INVOCATION_INTERRUPTED",
                    contract=contract.identity,
                )
            raise
        finally:
            try:
                if not invocation_returned:
                    terminal_error = sys.exception()
                    if terminal_error is not None and returned_execution is not None:
                        returned_execution["termination"]["reason"] = "post_execution_failure"
                        attach_failure(terminal_error, returned_execution)
                    if stage == "worker" and terminal_error is not None:
                        with suppress(Exception):
                            self._retain_partial(
                                run,
                                reservation,
                                workspace,
                                terminal_error,
                                diagnostic_boundary,
                                command_context,
                                extracted_tree,
                            )
                    self._failure_diagnostic(
                        run,
                        stage,
                        reservation,
                        diagnostic_boundary,
                        {
                            **command_context,
                            "commands": {
                                "capture_enabled": bool(capture_policy and capture_policy.enabled),
                                "reservation": reservation,
                                "meaning": "command context, not causal attribution",
                            },
                        },
                    )
            finally:
                self.journal.settle(run, reservation, time.monotonic() - started, usage)

    def _check_ids(self, criteria: tuple[Criterion, ...], config: RunConfig) -> None:
        defined = {check.id for check in config.checks}
        if any(not set(item.checks) <= defined for item in criteria):
            raise ValueError("UNDECLARED_VERIFICATION_CHECK")

    def _review(
        self,
        run: str,
        policy: StagePolicy,
        stage: str,
        proposal: Contract,
        original: dict[str, Any],
        workspace: Path,
        *,
        ambiguous: bool = False,
        external: bool = False,
    ) -> tuple[bool, str]:
        required = policy.review == "always" or (
            policy.review == "conditional"
            and (
                (ambiguous and "ambiguity" in policy.review_conditions)
                or (external and "external_authority" in policy.review_conditions)
            )
        )
        if not required:
            return True, "review disabled by snapshotted policy"
        reviewer = policy.model_copy(
            update={
                "model": policy.reviewer_model or policy.model,
                "backend": policy.reviewer_backend or policy.backend,
                "effort": policy.reviewer_effort or policy.effort,
            }
        )
        review = self._generate(
            run,
            stage + "_review",
            reviewer,
            {
                "instruction": "Independently review proposal against original. Reject omitted or "
                "weakened requirements or unsupported expansion. "
                "Use criterion_id=review.",
                "evidence_semantics": EVIDENCE["proposal_review"],
                "original": original,
                "proposal": proposal.model_dump(mode="json"),
                **(
                    {"source_evidence": proposal.source_evidence(self.journal.original(run))}
                    if isinstance(proposal, Clarification)
                    else {}
                ),
            },
            Verification,
            workspace,
        )
        accepted = review.accepts((Criterion(id="review", description="proposal is acceptable"),))
        self.journal.append(
            run,
            "review_diagnostic",
            stage=stage,
            target=proposal.digest,
            result_digest=review.digest,
            accepted=accepted,
            authoritative=False,
            summary="proposal accepted" if accepted else "proposal rejected",
            findings=[
                {
                    "criterion": item.criterion_id,
                    "passed": item.passed,
                    "category": item.category,
                    "evidence_digest": digest(item.evidence),
                }
                for item in review.findings
            ],
        )
        return accepted, review.summary

    def start(self, original: str, config: RunConfig, source: Path) -> str:
        run = self.prepare(original, config, source)
        self.execute(run)
        return run

    def prepare(self, original: str, config: RunConfig, source: Path) -> str:
        if not original.strip() or len(original) > 20000:
            raise ValueError("INVALID_ORIGINAL_INPUT")
        run = self.journal.create(original, config)
        tree = self.candidates.capture_source(source)
        self.journal.append(run, "input", tree=tree)
        return run

    def _clarify(self, run: str, config: RunConfig, tree: str) -> Goal:
        original = self.journal.original(run)
        feedback = ""
        workspace = self._workspace(run, "clarifier", tree)
        for _ in range(config.clarification.revisions + 1):
            proposal = self._generate(
                run,
                "clarification",
                config.clarification,
                {
                    "instruction": "Clarify the goal. Do not drop or weaken explicit requirements. "
                    "Map original_source reference IDs to criteria; do not copy quotations. "
                    "Preserve mandatory checks. "
                    "Use the shared clarification contract for investigation, unresolved needs "
                    "and questions; inspect the supplied input snapshot. "
                    "Use the shared evidence/outcome and method_selection semantics. "
                    "Do not invent authority.",
                    "original_input": original,
                    "clarification_answers": [
                        event["body"]["answer"]
                        for event in self.journal.events(run)
                        if event["kind"] == "clarification_answer"
                    ],
                    "mandatory_checks": config.mandatory_checks,
                    "available_checks": [check.model_dump(mode="json") for check in config.checks],
                    "feedback": feedback,
                },
                Clarification,
                workspace,
            )
            self._check_ids(proposal.criteria, config)
            accepted, feedback = self._review(
                run,
                config.clarification,
                "clarification",
                proposal,
                {"original_input": original, "mandatory_checks": config.mandatory_checks},
                workspace,
                ambiguous=proposal.disposition == "wait",
            )
            if accepted and proposal.disposition == "stop":
                self.journal.diagnostic(
                    run,
                    "clarification",
                    proposal.model_dump(mode="json"),
                    kind="clarification_environment_blocked",
                    target=proposal.digest,
                )
                raise Stopped("CLARIFICATION_ENVIRONMENT_BLOCKED")
            if accepted and proposal.disposition == "wait":
                self.journal.append(
                    run, "clarification_wait", proposal=proposal.model_dump(mode="json")
                )
                raise Waiting("WAITING_FOR_CLARIFICATION")
            if accepted:
                goal = Goal(
                    original_input=original,
                    specification=proposal,
                    mandatory_checks=config.mandatory_checks,
                )
                self.journal.append(run, "goal", goal=goal.model_dump(mode="json"))
                return goal
        raise ValueError("CLARIFICATION_REJECTED")

    def _readiness(self, run: str, task: Task, config: RunConfig, plan: Plan) -> None:
        try:
            self.journal.append(run, "readiness", **readiness(task, config))
        except Stopped:
            self.journal.diagnostic(
                run,
                "readiness",
                task_violation(task, config),
                kind="readiness_failed",
                task=task.id,
                task_digest=task.digest,
                target=plan.digest,
            )
            raise

    def _direct_task(self, run: str, task_id: str | None) -> bool:
        events = self.journal.events(run)
        starts = [e["body"] for e in events if e["kind"] == "direct_started"]
        plans = [e["body"]["plan"] for e in events if e["kind"] == "plan"]
        return bool(
            starts
            and plans
            and not any(e["kind"] == "direct_fallback" for e in events)
            and starts[-1]["plan"] == plans[-1]
            and task_id == plans[-1]["result_task"]
        )

    def _direct_remaining(self, run: str, config: RunConfig, observed: int = 0) -> float:
        self.journal.check(run)
        budget = config.direct_execution
        if budget is None:
            raise ValueError("DIRECT_CONFIGURATION_MISSING")
        events = self.journal.events(run)
        start = next(i for i, e in enumerate(events) if e["kind"] == "direct_started")
        ids = {
            e["body"]["id"]
            for i, e in enumerate(events)
            if i > start and e["kind"] == "reserved" and e["body"]["stage"] == "worker"
        }
        with self.journal.connect() as db:
            rows = db.execute("SELECT * FROM reservations WHERE run=?", (run,)).fetchall()
        # Recovered interrupted reservations remain charged; neither retries nor
        # controller restart grant another local allowance.
        seconds = sum(row["seconds"] for row in rows if row["id"] in ids and row["settled"])
        tokens = sum(row["tokens"] for row in rows if row["id"] in ids and row["settled"])
        if tokens + observed >= budget.tokens:
            raise DirectFallback("direct_token_budget")
        if seconds >= budget.seconds:
            raise DirectFallback("direct_time_budget")
        return float(budget.seconds - seconds)

    def _start_direct(self, run: str, goal: Goal, config: RunConfig) -> Plan | None:
        if (
            config.direct_execution is None
            or config.planning.review != "never"
            or config.worker_options
            or config.authority_ceiling != _NO_AUTHORITY
            or any(c.requires_external_evidence for c in goal.specification.criteria)
            or any(c.proves_external_effect for c in config.checks)
        ):
            return None
        task = Task(
            id="direct",
            description=goal.specification.clarified_goal,
            criteria=goal.specification.criteria,
            verification_plan="Independently inspect the candidate against every Goal criterion "
            "and all mandatory checks. Preserve the original authorized method alternatives.",
        )
        plan = Plan(tasks=(task,), result_task=task.id)
        self._readiness(run, task, config, plan)
        self._check_ids(task.criteria, config)
        self.journal.append_many(
            run,
            (
                (
                    "direct_started",
                    {
                        "plan": plan.model_dump(mode="json"),
                        "goal_digest": goal.digest,
                        "policy_digest": config.digest,
                    },
                ),
                ("plan", {"plan": plan.model_dump(mode="json")}),
            ),
        )
        return plan

    def _direct_handoff(self, run: str) -> dict[str, Any] | None:
        return next(
            (
                e["body"]
                for e in reversed(self.journal.events(run))
                if e["kind"] == "direct_fallback"
            ),
            None,
        )

    def _fallback_direct(
        self, run: str, goal: Goal, config: RunConfig, task: Task, reason: str, base: str
    ) -> None:
        self.journal.check(run)
        events = self.journal.events(run)
        if any(e["kind"] == "uncertain" for e in events):
            raise Waiting("UNCERTAIN_EXTERNAL_EFFECT")
        attempts = [
            e["body"]["context"]
            for e in events
            if e["kind"] == "attempt_started" and e["body"]["task_digest"] == task.digest
        ]
        summary = "No worker result was returned. Partial artifacts are unverified."
        if attempts:
            context = TaskContext.model_validate(attempts[-1])
            if context.authority != _NO_AUTHORITY:
                raise Waiting("DIRECT_AUTHORITY_CHANGED")
            workspace = Path(context.workspace)
            if workspace.is_symlink() or not workspace.resolve().is_relative_to(self.root / run):
                raise ValueError("WORKSPACE_UNAVAILABLE")
            base = self.candidates.capture(workspace)
            results = [
                e["body"]["result"]
                for e in events
                if e["kind"] == "worker_result" and e["body"]["attempt"] == context.attempt_id
            ]
            if results:
                summary = results[-1]["summary"][:2048]
        self.candidates.manifest(base)
        self.journal.append(
            run,
            "direct_fallback",
            tree=base,
            reason=reason,
            summary=summary,
            authoritative=False,
            goal_digest=goal.digest,
            policy_digest=config.digest,
            instruction="Continue from safe but unverified partial artifacts. "
            "Investigate the recorded failure; do not assume completion. "
            "This is local routing, not evidence of task complexity.",
        )
        self.journal.release_resources(run, task.digest)

    def _joint_eligible(
        self, run: str, config: RunConfig, goal: Goal, task: Task, candidate: Candidate
    ) -> bool:
        starts = [e["body"] for e in self.journal.events(run) if e["kind"] == "direct_started"]
        return bool(
            self._direct_task(run, task.id)
            and starts
            and starts[-1]["goal_digest"] == goal.digest
            and starts[-1]["policy_digest"] == config.digest
            and len(starts[-1]["plan"]["tasks"]) == 1
            and starts[-1]["plan"]["tasks"][0] == task.model_dump(mode="json")
            and task.criteria == goal.specification.criteria
            and set(goal.mandatory_checks) <= {key for c in task.criteria for key in c.checks}
            and task.authority == _NO_AUTHORITY
            and not task.dependencies
            and candidate.task_digest == task.digest
            and not candidate.upstream
            and candidate.authority_version == 0
            and not any(c.requires_external_evidence for c in task.criteria)
        )

    def _joint_record(
        self, run: str, config: RunConfig, goal: Goal, task: Task, candidate: Candidate
    ) -> dict[str, Any] | None:
        if not self._joint_eligible(run, config, goal, task, candidate):
            return None
        events = self.journal.events(run)
        for event in reversed(events):
            b = event["body"]
            if (
                event["kind"] != "verification"
                or b["goal_level"]
                or b["candidate"] != candidate.model_dump(mode="json")
            ):
                continue
            coverage = b.get("joint_coverage", {})
            if (
                not b["passed"]
                or coverage.get("goal_digest") != goal.digest
                or coverage.get("task_digest") != task.digest
                or coverage.get("policy_digest") != config.digest
                or not Verification.model_validate(b["result"]).accepts(goal.specification.criteria)
            ):
                return None
            receipts = coverage.get("checks", [])
            required = set(goal.mandatory_checks) | {key for c in task.criteria for key in c.checks}
            if {item["check"] for item in receipts} != required or any(
                not item["passed"]
                or item["candidate"] != candidate.digest
                or not any(
                    e["kind"] == "check_result"
                    and e["body"] == item
                    and events.index(e) < events.index(event)
                    for e in events
                )
                for item in receipts
            ):
                return None
            self._check_saved_comparison(run, goal, task, candidate)
            return event
        return None

    def _reuse_direct_verification(
        self, run: str, config: RunConfig, goal: Goal, task: Task, candidate: Candidate
    ) -> bool:
        event = self._joint_record(run, config, goal, task, candidate)
        if event is None:
            return False
        comparison = self._input_comparison(run, config, goal, None, candidate)
        self.journal.append(
            run,
            "verification",
            candidate=candidate.model_dump(mode="json"),
            goal_level=True,
            passed=True,
            result=event["body"]["result"],
            reused_from=digest(event),
            **({"input_comparison": comparison} if comparison is not None else {}),
        )
        return True

    def _plan(self, run: str, goal: Goal, config: RunConfig, tree: str) -> Plan:
        workspace = self._workspace(run, "planner", tree)
        feedback = ""
        for _ in range(config.planning.revisions + 1):
            plan = self._generate(
                run,
                "planning",
                config.planning,
                {
                    "instruction": "Plan one Task or a DAG. Use integration Tasks where branches "
                    "converge. Every Task must contribute to result_task. Define exact "
                    "success criteria, verification plan and required evidence. "
                    "Use the shared graph, evidence/outcome and method_selection semantics.",
                    "goal": goal.model_dump(mode="json"),
                    **(
                        {"direct_execution_handoff": self._direct_handoff(run)}
                        if self._direct_handoff(run) is not None
                        else {}
                    ),
                    "feedback": feedback,
                    "available_checks": [check.model_dump(mode="json") for check in config.checks],
                },
                Plan,
                workspace,
            )
            for task in plan.tasks:
                self._readiness(run, task, config, plan)
                self._check_ids(task.criteria, config)
                self._authorize(run, config, task.authority, task.digest)
            accepted, feedback = self._review(
                run,
                config.planning,
                "planning",
                plan,
                goal.model_dump(mode="json"),
                workspace,
                external=any(task.authority.external_writes for task in plan.tasks),
            )
            if accepted:
                self.journal.append(run, "plan", plan=plan.model_dump(mode="json"))
                return plan
        raise ValueError("PLAN_REJECTED")

    def _input_comparison(
        self, run: str, config: RunConfig, goal: Goal, task: Task | None, candidate: Candidate
    ) -> dict[str, Any] | None:
        criteria = goal.specification.criteria if task is None else task.criteria
        if not any(c.preserved_paths for c in criteria):
            return None
        initial = next(e["body"]["tree"] for e in self.journal.events(run) if e["kind"] == "input")
        source = {
            "run": run,
            "initial_tree": initial,
            "candidate": candidate.model_dump(mode="json"),
            "goal": goal.model_dump(mode="json"),
            "task": None if task is None else task.model_dump(mode="json"),
            "criteria": [c.model_dump(mode="json") for c in criteria],
        }
        return {
            "scope": StageContract.input_scope(source, config),
            "results": {
                c.id: self.candidates.compare_inputs(
                    initial, candidate.tree, c.preserved_paths, c.preservation_mode
                )
                for c in criteria
                if c.preserved_paths
            },
        }

    def _check_saved_comparison(
        self, run: str, goal: Goal, task: Task | None, candidate: Candidate
    ) -> None:
        expected = self._input_comparison(run, self.journal.config(run), goal, task, candidate)
        if expected is None:
            return
        record: dict[str, Any] = next(
            (
                e["body"]
                for e in reversed(self.journal.events(run))
                if e["kind"] == "verification"
                and e["body"]["goal_level"] == (task is None)
                and e["body"]["candidate"] == candidate.model_dump(mode="json")
            ),
            {},
        )
        if record.get("input_comparison") != expected or not all(
            r["matches"] for r in expected["results"].values()
        ):
            raise ValueError("INPUT_COMPARISON_CONTEXT_MISMATCH")

    def _verify(
        self,
        run: str,
        config: RunConfig,
        goal: Goal,
        task: Task | None,
        candidate: Candidate,
        result: WorkerResult | None,
    ) -> bool:
        criteria = goal.specification.criteria if task is None else task.criteria
        check_ids = (set(goal.mandatory_checks) if task is None else set()) | {
            key for item in criteria for key in item.checks
        }
        receipts: list[dict[str, Any]] = []
        for check in config.checks:
            if check.id not in check_ids:
                continue
            check_workspace = self._workspace(run, "check", candidate.tree)
            try:
                reservation, timeout = self.journal.reserve(run, "check", model_usage=False)
            except Stopped as error:
                self.journal.stop(run, str(error))
                raise
            started = time.monotonic()
            check_recorded = False
            diagnostic_boundary = sys.exception()
            try:
                passed, output = self.model.check(
                    check.argv,
                    check_workspace,
                    minimum(check.timeout, timeout),
                    lambda: self._cancelled(run),
                )
                self.journal.diagnostic(
                    run,
                    "check",
                    output.payload() if isinstance(output, CheckOutput) else output,
                    reservation=reservation,
                    check=check.id,
                    candidate=candidate.digest,
                    attempt=candidate.attempt_id,
                    task=None if task is None else task.id,
                    kind="check_output",
                    passed=passed,
                )
                evidence = output.digest if isinstance(output, CheckOutput) else output
                check_recorded = True
            except (ValueError, RuntimeError, OSError, TimeoutError) as error:
                self.journal.diagnostic(
                    run,
                    "check",
                    {
                        "error_type": type(error).__name__,
                        "reason": str(error),
                        "output_unavailable": True,
                    },
                    reservation=reservation,
                    check=check.id,
                    candidate=candidate.digest,
                    attempt=candidate.attempt_id,
                    task=None if task is None else task.id,
                    kind="check_failed",
                )
                self.journal.check(run)
                raise
            finally:
                try:
                    if not check_recorded:
                        self._failure_diagnostic(run, "check", reservation, diagnostic_boundary)
                finally:
                    self.journal.settle(
                        run, reservation, time.monotonic() - started, Usage(tokens=0, cost=0)
                    )
            self.journal.check(run)
            receipt = {
                "check": check.id,
                "evidence_kind": check.evidence_kind,
                "passed": passed,
                "evidence": evidence,
                "candidate": candidate.digest,
            }
            receipts.append(receipt)
            self.journal.append(run, "check_result", **receipt)
        events = self.journal.events(run)
        external = any(criterion.requires_external_evidence for criterion in criteria) or any(
            event["kind"] == "attempt_started"
            and event["body"]["context"]["authority"]["external_writes"]
            and (task is None or event["body"]["context"]["attempt_id"] == candidate.attempt_id)
            for event in events
        )
        adopted = {
            Candidate.model_validate(event["body"]["candidate"]).digest
            for event in events
            if event["kind"] == "accepted"
        }
        prior_checks = [
            event["body"]
            for event in events
            if task is None
            and event["kind"] == "check_result"
            and event["body"]["passed"]
            and event["body"]["candidate"] in adopted
        ]
        verification_stage = "goal_verification" if task is None else "task_verification"
        boundary = LIFECYCLE[verification_stage]
        joint = task is not None and self._joint_eligible(run, config, goal, task, candidate)
        verification_context = {
            "verification_scope": verification_stage,
            "goal": goal.model_dump(mode="json"),
            "task": None if task is None else task.model_dump(mode="json"),
            "criteria": [item.model_dump(mode="json") for item in criteria],
            "candidate": candidate.model_dump(mode="json"),
            "checks": receipts,
            "upstream_check_evidence": prior_checks,
            "worker_result": None if result is None else result.model_dump(mode="json"),
        }
        if joint:
            verification_context["joint_goal_coverage"] = {
                "goal_digest": goal.digest,
                "instruction": "Independently verify the entire Goal as well as this direct Task. "
                "Their criteria are identical. One result supplies evidence for two separate "
                "runtime acceptance decisions; do not attest future or external effects.",
            }
        comparison = self._input_comparison(run, config, goal, task, candidate)
        if comparison is not None:
            verification_context.update(
                run=run,
                initial_tree=comparison["scope"]["initial_tree"],
                input_comparison=comparison,
            )
        feedback = ""
        passed = False
        for _ in range(config.verification.revisions + 1):
            verification = self._generate(
                run,
                verification_stage,
                config.verification,
                {
                    "instruction": EVIDENCE["verification"],
                    **verification_context,
                    "review_feedback": feedback,
                },
                Verification,
                self._workspace(run, "verification", candidate.tree),
            )
            reviewed, feedback = self._review(
                run,
                config.verification,
                "verification",
                verification,
                verification_context,
                self._workspace(run, "verification-review", candidate.tree),
                external=external,
            )
            if reviewed:
                passed = verification.accepts(criteria) and all(item["passed"] for item in receipts)
                if comparison is not None:
                    passed = passed and all(r["matches"] for r in comparison["results"].values())
                # Generic TLS observation cannot attest remote operation semantics.
                # Without a real service read provider, require an operator-owned
                # evidence check (e.g. signed receipt validation), never only prose.
                external_receipts = [
                    item
                    for item in (*receipts, *prior_checks)
                    if external_evidence(item.get("evidence_kind")) and item["passed"]
                ]
                if external and not external_receipts:
                    passed = False
                    self.journal.append(
                        run, "external_evidence_missing", candidate=candidate.digest
                    )
                break
        self.candidates.manifest(candidate.tree)
        self.journal.append(
            run,
            "verification",
            candidate=candidate.model_dump(mode="json"),
            goal_level=boundary["goal_level"],
            passed=passed,
            result=verification.model_dump(mode="json"),
            **({"input_comparison": comparison} if comparison is not None else {}),
            **(
                {
                    "joint_coverage": {
                        "goal_digest": goal.digest,
                        "task_digest": task.digest,
                        "policy_digest": config.digest,
                        "checks": receipts,
                    }
                }
                if joint and task is not None
                else {}
            ),
        )
        return passed

    def _authorize(
        self, run: str, config: RunConfig, authority: Authority, task_digest: str
    ) -> None:
        reason = authority_policy_failure(authority, config)
        if reason is not None:
            self.journal.append(run, "policy_denied", task_digest=task_digest, reason=reason)
            raise Stopped(reason)
        if authority.external_writes and config.security != "strict":
            self.journal.append(
                run,
                "coarse_authority_allowed",
                task_digest=task_digest,
                policy=config.security,
                authority=authority.model_dump(mode="json"),
                guarantee="resource boundary only; no per-operation approval or SDK retry control",
            )

    def _select_worker(self, run: str, task: Task, config: RunConfig) -> StagePolicy:
        selector = config.selection or config.planning
        workspace = self._workspace(run, "selection")
        original = {
            "task": task.model_dump(mode="json"),
            "options": [policy.model_dump(mode="json") for policy in config.worker_options],
        }
        feedback = ""
        for _ in range(selector.revisions + 1):
            selection = self._generate(
                run,
                "selection",
                selector,
                {
                    "instruction": "Select the best zero-based worker option for this Task.",
                    **original,
                    "feedback": feedback,
                },
                WorkerChoice,
                workspace,
            )
            accepted, feedback = self._review(
                run,
                selector,
                "selection",
                selection,
                original,
                workspace,
                external=task.authority.external_writes,
            )
            if accepted:
                return config.worker_options[selection.index]
        raise ValueError("WORKER_SELECTION_REJECTED")

    def _resume_completed_attempt(
        self,
        run: str,
        context: TaskContext,
        goal: Goal,
        config: RunConfig,
        policy: StagePolicy,
    ) -> Candidate | None:
        """Recover a completed worker without repeating its implementation or external effects."""
        events = self.journal.events(run)
        results = [
            WorkerResult.model_validate(event["body"]["result"])
            for event in events
            if event["kind"] == "worker_result" and event["body"]["attempt"] == context.attempt_id
        ]
        if not results or results[-1].action(context.authority.external_writes) != "verify":
            return None
        if any(
            event["kind"] == "attempt_failed" and event["body"]["attempt"] == context.attempt_id
            for event in events
        ):
            return None
        if context.goal.digest != goal.digest:
            raise ValueError("STALE_GOAL_CONTEXT")
        frozen = [
            Candidate.model_validate(event["body"]["candidate"])
            for event in events
            if event["kind"] == "candidate"
            and event["body"]["candidate"]["attempt_id"] == context.attempt_id
        ]
        if frozen:
            candidate = frozen[-1]
            if (
                candidate.task_digest != context.task.digest
                or candidate.authority_version != context.authority_version
                or candidate.upstream != tuple(item.digest for item in context.upstream)
            ):
                raise ValueError("CANDIDATE_CONTEXT_CHANGED")
            self.candidates.manifest(candidate.tree)
        else:
            # A completed result is recorded only after the adapter returned its
            # quiesced workspace. Capture may safely finish after controller failure.
            candidate = self.candidates.freeze(context)
            self.journal.append(run, "candidate", candidate=candidate.model_dump(mode="json"))
        verified = [
            event["body"]["passed"]
            for event in events
            if event["kind"] == "verification"
            and not event["body"]["goal_level"]
            and Candidate.model_validate(event["body"]["candidate"]) == candidate
        ]
        if verified:
            accepted = verified[-1]
            if accepted:
                self._check_saved_comparison(run, goal, context.task, candidate)
        else:
            reviewed, _ = self._review(
                run,
                policy,
                "worker",
                results[-1],
                {
                    "goal": goal.model_dump(mode="json"),
                    "task": context.task.model_dump(mode="json"),
                    "candidate": candidate.model_dump(mode="json"),
                },
                self._workspace(run, "worker-review", candidate.tree),
                external=context.authority.external_writes,
            )
            accepted = reviewed and self._verify(
                run, config, goal, context.task, candidate, results[-1]
            )
        if accepted:
            self.journal.append_many(
                run,
                (
                    (
                        LIFECYCLE["task_verification"]["postcondition"],
                        {"task": context.task.id, "candidate": candidate.model_dump(mode="json")},
                    ),
                    (
                        "candidate_reused",
                        {
                            "attempt": context.attempt_id,
                            "candidate": candidate.digest,
                            "reason": "completed_worker_recovered_without_reexecution",
                        },
                    ),
                ),
            )
            self.journal.release_resources(run, context.task.digest)
            return candidate
        if context.authority.external_writes:
            self.journal.append(
                run,
                "uncertain",
                task_digest=context.task.digest,
                attempt=context.attempt_id,
                reason="EXTERNAL_RESULT_UNVERIFIED",
            )
            raise Waiting("UNVERIFIED_EXTERNAL_EFFECT")
        self.journal.append(
            run,
            "attempt_failed",
            attempt=context.attempt_id,
            reason="RECOVERED_VERIFICATION_FAILED",
        )
        return None

    def _execute_task(
        self,
        run: str,
        task: Task,
        goal: Goal,
        config: RunConfig,
        upstream: tuple[Candidate, ...],
        base: str,
    ) -> Candidate:
        direct = self._direct_task(run, task.id)
        # Scheduler inputs must be exact currently accepted upstream Candidates.
        plans = [e["body"]["plan"] for e in self.journal.events(run) if e["kind"] == "plan"]
        current = Plan.model_validate(plans[-1])
        accepted_inputs = self._accepted(run, current)
        if tuple(accepted_inputs.get(key) for key in task.dependencies) != upstream:
            raise Stopped("STALE_UPSTREAM_LINEAGE")
        readiness(task, config)
        prior = [
            event
            for event in self.journal.events(run)
            if event["kind"] == "attempt_started" and event["body"]["task_digest"] == task.digest
        ]
        choices = [
            event["body"]
            for event in self.journal.events(run)
            if event["kind"] == "worker_selected" and event["body"]["task_digest"] == task.digest
        ]
        worker_policy = config.worker
        if choices:
            worker_policy = StagePolicy.model_validate(choices[-1]["policy"])
            if worker_policy not in (config.worker, *config.worker_options):
                raise ValueError("FOREIGN_WORKER_SELECTION")
        elif config.worker_options:
            worker_policy = self._select_worker(run, task, config)
        if not choices:
            self.journal.append(
                run,
                "worker_selected",
                task_digest=task.digest,
                policy=worker_policy.model_dump(mode="json"),
            )
        authority = task.authority
        self._authorize(run, config, authority, task.digest)
        version = 0
        feedback = ""
        applied = [
            event["body"]
            for event in self.journal.events(run)
            if event["kind"] == "authority_applied" and event["body"]["task_digest"] == task.digest
        ]
        if applied:
            authority = Authority.model_validate(applied[-1]["authority"])
            version = applied[-1]["version"]
        if not self.journal.acquire_resources(run, task.digest, authority):
            raise Waiting("WAITING_FOR_EXTERNAL_RESOURCE")
        if prior:
            previous = TaskContext.model_validate(prior[-1]["body"]["context"])
            workspace = Path(previous.workspace)
            if (
                workspace.is_symlink()
                or not workspace.resolve().is_relative_to(self.root / run)
                or not workspace.is_dir()
            ):
                self.journal.stop(run, "WORKSPACE_UNAVAILABLE")
                raise Stopped("WORKSPACE_UNAVAILABLE")
            if not applied:
                authority, version = previous.authority, previous.authority_version
            if previous.upstream != upstream or previous.task.digest != task.digest:
                raise ValueError("STALE_UPSTREAM_LINEAGE")
            resumed = self._resume_completed_attempt(
                run, previous, goal, config, StagePolicy.model_validate(prior[-1]["body"]["worker"])
            )
            if resumed is not None:
                return resumed
            if direct:
                raise DirectFallback("interrupted_or_rejected_direct_attempt")
            # An externally writable invocation without an accepted result may
            # have committed remotely before controller/process failure.
            if previous.authority.external_writes:
                self.journal.append(
                    run,
                    "uncertain",
                    task_digest=task.digest,
                    attempt=previous.attempt_id,
                    reason="INTERRUPTED_EXTERNAL_ATTEMPT",
                )
                raise Waiting("UNCERTAIN_EXTERNAL_EFFECT")
        else:
            workspace = self._workspace(
                run, "worker", upstream[0].tree if len(upstream) == 1 else base
            )
        if len(upstream) > 1 and not prior:
            inputs = workspace / ".fleet-inputs"
            for index, candidate in enumerate(upstream):
                self.candidates.materialize(candidate.tree, inputs / str(index))
        attempts = {item["body"]["context"]["attempt_id"] for item in prior}
        failures = sum(
            event["kind"] == "attempt_failed" and event["body"].get("attempt") in attempts
            for event in self.journal.events(run)
        )
        for _ in range(len(prior), 1 if direct else config.limits.task_attempts):
            if failures and config.worker_escalations:
                worker_policy = config.worker_escalations[
                    min(failures - 1, len(config.worker_escalations) - 1)
                ]
            attempt = "worker-" + uuid4().hex
            context = TaskContext(
                goal=goal,
                task=task,
                attempt_id=attempt,
                upstream=upstream,
                workspace=str(workspace),
                authority=authority,
                authority_version=version,
            )
            self.journal.append(
                run,
                "attempt_started",
                task_digest=task.digest,
                context=context.model_dump(mode="json"),
                worker=worker_policy.model_dump(mode="json"),
            )
            try:
                result = self._generate(
                    run,
                    "worker",
                    worker_policy,
                    {
                        "instruction": "Complete the Task autonomously in the granted environment. "
                        "Inspect, edit, run tests, observe and repair as useful. Return "
                        "status/evidence, not edit proposals. Integration inputs, when "
                        "present, are read-only in .fleet-inputs/<index>; integrate into "
                        "the workspace without references to environment paths in deliverables. "
                        "Request missing authority; never embed Fleet bypasses. "
                        "Stop on usage limit; never redeem resets or buy allowance.",
                        "context": context.model_dump(mode="json"),
                        "feedback": feedback,
                        **(
                            {
                                "direct_execution": {
                                    "instruction": "Complete the clarified Goal directly. "
                                    "Choose among user-authorized methods without narrowing"
                                    " requirements. "
                                    "If you cannot finish within the local allowance, "
                                    "return failed with "
                                    "a concise progress summary; normal planning can "
                                    "continue from safe "
                                    "partial artifacts. Do not request extra authority in "
                                    "this attempt.",
                                    "budget": config.direct_execution.model_dump(mode="json"),
                                    "clarification_observations": {
                                        "input_tree": base,
                                        "policy_digest": config.digest,
                                        "authority": _NO_AUTHORITY.model_dump(mode="json"),
                                        "source": "context.goal.specification.observations",
                                        "authoritative": False,
                                        "instruction": "Original-snapshot reports; "
                                        "reuse relevant investigation, but recheck changed or "
                                        "unsupported facts.",
                                    },
                                }
                            }
                            if direct and config.direct_execution is not None
                            else {}
                        ),
                    },
                    WorkerResult,
                    workspace,
                    authority,
                )
                action = result.action(authority.external_writes)
                if action == "stop":
                    self.journal.stop(run, "USAGE_LIMIT")
                    raise Stopped("USAGE_LIMIT")
                if action != "request":
                    self.journal.append(
                        run, "worker_result", attempt=attempt, result=result.model_dump(mode="json")
                    )
                if action == "uncertain":
                    self.journal.append(
                        run,
                        "uncertain",
                        task_digest=task.digest,
                        attempt=attempt,
                        result=result.model_dump(mode="json"),
                    )
                    raise Waiting("UNCERTAIN_EXTERNAL_EFFECT")
                if action == "request" and direct:
                    self.journal.append(
                        run, "worker_result", attempt=attempt, result=result.model_dump(mode="json")
                    )
                    raise DirectFallback("direct_authority_required")
                if action == "request":
                    requested = result.authority_request
                    if requested is None or not requested.within(config.authority_ceiling):
                        raise ValueError("AUTHORITY_REQUEST_DENIED")
                    self.journal.append_many(
                        run,
                        (
                            (
                                "worker_result",
                                {"attempt": attempt, "result": result.model_dump(mode="json")},
                            ),
                            (
                                "authority_requested",
                                {
                                    "attempt": attempt,
                                    "requested": requested.model_dump(mode="json"),
                                    "version": version + 1,
                                },
                            ),
                            (
                                "approval_wait",
                                {
                                    "attempt": attempt,
                                    "workspace": str(workspace),
                                    "task_digest": task.digest,
                                },
                            ),
                        ),
                    )
                    raise Waiting("PAUSED_FOR_APPROVAL")
                if action != "verify":
                    feedback = result.summary
                    self.journal.append(
                        run, "attempt_failed", attempt=attempt, reason="WORKER_FAILED"
                    )
                    failures += 1
                    continue
                candidate = self.candidates.freeze(context)
                self.journal.append(run, "candidate", candidate=candidate.model_dump(mode="json"))
                reviewed, review_feedback = self._review(
                    run,
                    worker_policy,
                    "worker",
                    result,
                    {
                        "goal": goal.model_dump(mode="json"),
                        "task": task.model_dump(mode="json"),
                        "candidate": candidate.model_dump(mode="json"),
                    },
                    self._workspace(run, "worker-review", candidate.tree),
                    external=authority.external_writes,
                )
                if reviewed and self._verify(run, config, goal, task, candidate, result):
                    self.journal.append(
                        run,
                        LIFECYCLE["task_verification"]["postcondition"],
                        task=task.id,
                        candidate=candidate.model_dump(mode="json"),
                    )
                    self.journal.release_resources(run, task.digest)
                    return candidate
                if authority.external_writes:
                    self.journal.append(
                        run,
                        "uncertain",
                        task_digest=task.digest,
                        attempt=attempt,
                        reason="EXTERNAL_RESULT_UNVERIFIED",
                    )
                    raise Waiting("UNVERIFIED_EXTERNAL_EFFECT")
                evidence = [
                    event["body"]
                    for event in self.journal.events(run)
                    if event["kind"] in {"verification", "check_result"}
                ][-10:]
                feedback = (
                    "Independent verification rejected this candidate. Repair using: "
                    + json.dumps(
                        {"review": review_feedback, "evidence": evidence}, ensure_ascii=False
                    )
                )
                self.journal.append(
                    run, "attempt_failed", attempt=attempt, reason="VERIFICATION_FAILED"
                )
                failures += 1
            except Stopped:
                if authority.external_writes:
                    self.journal.append(
                        run,
                        "uncertain",
                        task_digest=task.digest,
                        attempt=attempt,
                        reason="STOPPED_EXTERNAL_ATTEMPT",
                    )
                raise
            except (ValueError, TimeoutError) as error:
                # For externally writable sessions a transport failure may have followed a commit.
                if authority.external_writes:
                    self.journal.append(
                        run,
                        "uncertain",
                        task_digest=task.digest,
                        attempt=attempt,
                        reason=type(error).__name__,
                    )
                    raise Waiting("UNCERTAIN_EXTERNAL_EFFECT") from error
                feedback = str(error)
                self.journal.append(run, "attempt_failed", attempt=attempt, reason=feedback[:200])
                failures += 1
        if direct:
            raise DirectFallback("direct_not_completed")
        raise ValueError("TASK_ATTEMPTS_EXHAUSTED")

    def execute(self, run: str) -> None:
        with self.journal.controller(run):
            try:
                self.model.reconcile(self.root / run)
                self.journal.recover_reservations(run)
                self._execute(run)
            except Waiting:
                return
            except KeyboardInterrupt:
                self.journal.stop(run, "OPERATOR_CANCELLED")
                raise
            except Stopped as error:
                self.journal.stop(run, str(error))
                raise
            except (ValueError, TimeoutError, RuntimeError, OSError) as error:
                self.journal.append(run, "failed", reason=str(error)[:200])
                raise

    def cleanup(self, run: str) -> None:
        """Reconcile owned runtime resources without model calls or deleting history.

        A busy controller observes the persisted stop. Call again after it exits;
        a request is never reported as confirmed release.
        """
        self.journal.request_cleanup(run)
        with self.journal.controller(run):
            try:
                self.model.reconcile(self.root / run)
                self.journal.recover_reservations(run)
            except (ValueError, RuntimeError, OSError, TimeoutError) as error:
                known = {
                    "INVALID_RESOURCE_LEDGER",
                    "RESOURCE_CLEANUP_UNCONFIRMED",
                    "RESOURCE_CREATION_UNCERTAIN",
                }
                self.journal.append(
                    run,
                    "cleanup_failed",
                    error_type=type(error).__name__,
                    reason=str(error) if str(error) in known else "RESOURCE_CLEANUP_UNCONFIRMED",
                )
                raise
            self.journal.append(run, "cleanup_confirmed")

    def result(self, run: str) -> Candidate:
        """Return the exact verified final Candidate under publication authority."""
        with self.journal.controller(run):
            if any(
                event["kind"] in {"stopped", "uncertain", "authority_revoked"}
                for event in self.journal.events(run)
            ):
                raise ValueError("PROMOTION_AUTHORITY_UNAVAILABLE")
            return self._completion(run)

    def revise_goal(self, run: str, original: str) -> str:
        """An explicit human replacement starts a linked Run; automatic recovery cannot use it."""
        if not original.strip() or len(original) > 20000:
            raise ValueError("INVALID_ORIGINAL_INPUT")
        with self.journal.controller(run):
            self.journal.config(run)
            events = self.journal.events(run)
            if any(event["kind"] in {"stopped", "uncertain"} for event in events):
                raise Stopped("TERMINAL_RUN_CANNOT_BE_REVISED")
            self.model.reconcile(self.root / run)
            self.journal.recover_reservations(run)
            completed = any(event["kind"] == "completed" for event in events)
            tree = (
                self._completion(run).tree
                if completed
                else next(event["body"]["tree"] for event in events if event["kind"] == "input")
            )
            return self.journal.supersede(run, original, tree)

    def answer(self, run: str, answer: str) -> None:
        """Record explicit clarification input without changing original provenance."""
        if not answer.strip():
            raise ValueError("EMPTY_CLARIFICATION_ANSWER")
        with self.journal.controller(run):
            relevant = [
                event
                for event in self.journal.events(run)
                if event["kind"] in {"clarification_wait", "clarification_answer", "goal"}
            ]
            if not relevant or relevant[-1]["kind"] != "clarification_wait":
                raise ValueError("NO_PENDING_CLARIFICATION")
            self.journal.append(run, "clarification_answer", answer=answer)

    def revoke_authority(self, run: str) -> None:
        """Revocation stops this Run; it never claims to mutate a live native session.

        A busy controller observes the persisted stop and tears down its children.
        Repeating this command after it exits records mechanical completion.
        """
        self.journal.config(run)
        self.journal.stop(run, "AUTHORITY_REVOKED")
        self.journal.append(run, "authority_revocation_requested")
        with self.journal.controller(run):
            self.model.reconcile(self.root / run)
            self.journal.recover_reservations(run)
            self.journal.append(run, "authority_revoked", scope="all_task_sessions", resume=False)

    def approve_authority(self, run: str, attempt: str, *, approve: bool) -> None:
        with self.journal.controller(run):
            self.journal.check(run)
            events = self.journal.events(run)
            requests = [
                event["body"]
                for event in events
                if event["kind"] == "authority_requested" and event["body"]["attempt"] == attempt
            ]
            waits = [
                event["body"]
                for event in events
                if event["kind"] == "approval_wait" and event["body"]["attempt"] == attempt
            ]
            if (
                not requests
                or not waits
                or any(
                    event["kind"] in {"authority_applied", "authority_rejected"}
                    and event["body"].get("attempt") == attempt
                    for event in events
                )
            ):
                raise ValueError("NO_PENDING_AUTHORITY_REQUEST")
            request, wait = requests[-1], waits[-1]
            if not approve:
                self.journal.append(run, "authority_rejected", attempt=attempt)
                self.journal.stop(run, "AUTHORITY_REJECTED")
                return
            authority = Authority.model_validate(request["requested"])
            config = self.journal.config(run)
            self._authorize(run, config, authority, wait["task_digest"])
            contexts = [
                TaskContext.model_validate(e["body"]["context"])
                for e in events
                if e["kind"] == "attempt_started" and e["body"]["context"]["attempt_id"] == attempt
            ]
            if not contexts:
                raise Stopped("AUTHORITY_CONTEXT_MISSING")
            # A newly requested external grant changes the evidence required to
            # finish safely. Confirm that route before applying the grant.
            readiness(contexts[-1].task.model_copy(update={"authority": authority}), config)
            workspace = Path(wait["workspace"])
            if not workspace.resolve().is_relative_to(self.root / run):
                raise ValueError("FOREIGN_WORKSPACE")
            self.model.reconcile(self.root / run)
            self.journal.recover_reservations(run)
            self.journal.append(
                run, "authority_approved", attempt=attempt, version=request["version"]
            )
            # No live Worker exists at this boundary. New native sessions use
            # confirmed policy and the retained workspace on continuation.
            if not self.journal.acquire_resources(run, wait["task_digest"], authority):
                raise Waiting("WAITING_FOR_EXTERNAL_RESOURCE")
            try:
                reservation, timeout = self.journal.reserve(
                    run, "authority_application", model_usage=False
                )
            except Stopped as error:
                self.journal.stop(run, str(error))
                raise
            started = time.monotonic()
            try:
                self.model.apply_authority(
                    workspace, authority, timeout, lambda: self._cancelled(run)
                )
            except (TimeoutError, ConnectionError):
                self.journal.check(run)
                raise
            finally:
                self.journal.settle(
                    run, reservation, time.monotonic() - started, Usage(tokens=0, cost=0)
                )
            self.journal.check(run)
            self.journal.append(
                run,
                "authority_applied",
                attempt=attempt,
                task_digest=wait["task_digest"],
                version=request["version"],
                authority=authority.model_dump(mode="json"),
            )

    def _execute(self, run: str) -> None:
        config = self.journal.config(run)
        for policy in policies(config):
            validate_policy(policy)
        events = self.journal.events(run)
        if events[0]["body"].get("contract_version") != VERSION:
            raise Stopped("CONTRACT_VERSION_CHANGED")
        if any(event["kind"] == "stopped" for event in events):
            raise Stopped("RUN_STOPPED")
        if any(event["kind"] == "completed" for event in events):
            self._completion(run)
            return
        self.journal.check(run)
        waiting: set[str] = set()
        clarification_pending = False
        for event in events:
            if event["kind"] == "approval_wait":
                waiting.add(event["body"]["attempt"])
            elif event["kind"] in {"authority_applied", "authority_rejected"}:
                waiting.discard(event["body"]["attempt"])
            elif event["kind"] == "clarification_wait":
                clarification_pending = True
            elif event["kind"] == "clarification_answer":
                clarification_pending = False
        if (
            waiting
            or clarification_pending
            or any(event["kind"] == "uncertain" for event in events)
        ):
            raise Waiting("UNRESOLVED_INPUT_OR_EFFECT")
        base = next(event["body"]["tree"] for event in events if event["kind"] == "input")
        goals = [event for event in events if event["kind"] == "goal"]
        goal = (
            Goal.model_validate(goals[-1]["body"]["goal"])
            if goals
            else self._clarify(run, config, base)
        )
        if (
            goal.original_input != self.journal.original(run)
            or goal.mandatory_checks != config.mandatory_checks
        ):
            raise ValueError("GOAL_PROVENANCE_CHANGED")
        plans = [event for event in events if event["kind"] == "plan"]
        if plans:
            plan = Plan.model_validate(plans[-1]["body"]["plan"])
        else:
            plan = self._start_direct(run, goal, config) or self._plan(run, goal, config, base)
        handoff = self._direct_handoff(run)
        if handoff is not None:
            if handoff["goal_digest"] != goal.digest or handoff["policy_digest"] != config.digest:
                raise ValueError("DIRECT_HANDOFF_CONTEXT_CHANGED")
            base = handoff["tree"]
            current_events = self.journal.events(run)
            last_plan = next(e for e in reversed(current_events) if e["kind"] == "plan")
            fallback = next(e for e in reversed(current_events) if e["kind"] == "direct_fallback")
            if current_events.index(last_plan) < current_events.index(fallback):
                plan = self._plan(run, goal, config, base)
        tasks = {task.id: task for task in plan.tasks}
        accepted = self._accepted(run, plan)
        # Acceptance is durable even if the controller died before releasing its lease.
        for candidate in accepted.values():
            self.journal.release_resources(run, candidate.task_digest)
        pending = set(tasks) - set(accepted)
        while pending:
            self.journal.check(run)
            ready = sorted(key for key in pending if set(tasks[key].dependencies) <= set(accepted))
            if not ready:
                raise ValueError("GRAPH_CANNOT_PROGRESS")
            failure: str | None = None
            with ThreadPoolExecutor(max_workers=config.limits.concurrency) as pool:
                futures = {
                    pool.submit(
                        self._execute_task,
                        run,
                        tasks[key],
                        goal,
                        config,
                        tuple(accepted[parent] for parent in tasks[key].dependencies),
                        base,
                    ): key
                    for key in ready[: config.limits.concurrency]
                }
                try:
                    for future in as_completed(futures):
                        key = futures[future]
                        try:
                            accepted[key] = future.result()
                            pending.remove(key)
                        except DirectFallback as error:
                            self._fallback_direct(run, goal, config, tasks[key], str(error), base)
                            return self._execute(run)
                        except (ValueError, TimeoutError) as error:
                            failure = str(error)
                            self.journal.append(run, "task_failed", task=key, reason=failure[:200])
                except KeyboardInterrupt:
                    self.journal.stop(run, "OPERATOR_CANCELLED")
                    raise
            if failure is not None:
                self._recover(run, config, goal, plan, accepted, failure, base)
                return self._execute(run)
        final = accepted[plan.result_task]
        if not (
            self._reuse_direct_verification(run, config, goal, tasks[plan.result_task], final)
            or self._verify(run, config, goal, None, final, None)
        ):
            self.journal.append(run, "goal_rejected", candidate=final.model_dump(mode="json"))
            if any(task.authority.external_writes for task in plan.tasks):
                self.journal.append(run, "uncertain", reason="EXTERNAL_GOAL_UNVERIFIED")
                raise Waiting("UNVERIFIED_EXTERNAL_EFFECT")
            self._recover(run, config, goal, plan, accepted, "GOAL_VERIFICATION_FAILED", final.tree)
            return self._execute(run)
        self.journal.check(run)
        self.journal.append(
            run,
            LIFECYCLE["goal_verification"]["postcondition"],
            candidate=final.model_dump(mode="json"),
            goal_digest=goal.digest,
        )

    def _recover(
        self,
        run: str,
        config: RunConfig,
        goal: Goal,
        previous: Plan,
        accepted: dict[str, Candidate],
        failure: str,
        tree: str,
    ) -> None:
        events = self.journal.events(run)
        plans = [
            Plan.model_validate(event["body"]["plan"])
            for event in events
            if event["kind"] == "plan"
        ]
        if len(plans) - 1 >= config.limits.replans:
            raise ValueError("REPLAN_LIMIT_EXHAUSTED")
        workspace = self._workspace(run, "recovery", tree)
        evidence = [
            event
            for event in events
            if event["kind"] in {"verification", "attempt_failed", "task_failed", "goal_rejected"}
        ][-20:]
        recovery_context = {
            "goal": goal.model_dump(mode="json"),
            "previous_plan": previous.model_dump(mode="json"),
            "original_task_ids": [task.id for task in plans[0].tasks],
            "failed_tasks": sorted(
                {e["body"]["task"] for e in events if e["kind"] == "task_failed"} - accepted.keys()
            ),
            "historical_tasks": [
                t.model_dump(mode="json")
                for t in {t.id: t for p in plans for t in p.tasks}.values()
            ],
            "available_checks": [c.model_dump(mode="json") for c in config.checks],
            "accepted": {key: value.model_dump(mode="json") for key, value in accepted.items()},
            "failure": failure,
            "evidence": evidence,
        }
        feedback = ""
        for _ in range(config.recovery.revisions + 1):
            extension = self._generate(
                run,
                "recovery",
                config.recovery,
                {
                    "instruction": GRAPH["recovery"],
                    **recovery_context,
                    "review_feedback": feedback,
                },
                Plan,
                workspace,
            )
            reviewed, feedback = self._review(
                run,
                config.recovery,
                "recovery",
                extension,
                recovery_context,
                workspace,
                external=any(task.authority.external_writes for task in extension.tasks),
            )
            if reviewed:
                break
        else:
            raise ValueError("RECOVERY_REJECTED")
        historical = {task.id: task for plan in plans for task in plan.tasks}
        new = {task.id for task in extension.tasks} - historical.keys()
        for task in extension.tasks:
            self._readiness(run, task, config, extension)
            self._check_ids(task.criteria, config)
            self._authorize(run, config, task.authority, task.digest)
        self.journal.append(
            run,
            "plan",
            plan=extension.model_dump(mode="json"),
            previous=previous.digest,
            reason=failure,
            added=sorted(new),
        )

    def _accepted(self, run: str, plan: Plan) -> dict[str, Candidate]:
        tasks = {task.id: task for task in plan.tasks}
        events = self.journal.events(run)
        accepted: dict[str, Candidate] = {}
        for event in events:
            if event["kind"] == "accepted" and event["body"]["task"] in tasks:
                key = event["body"]["task"]
                candidate = Candidate.model_validate(event["body"]["candidate"])
                if candidate.task_digest != tasks[key].digest:
                    raise ValueError("FOREIGN_TASK_CANDIDATE")
                accepted[key] = candidate
        for key, candidate in accepted.items():
            parents = tasks[key].dependencies
            if any(parent not in accepted for parent in parents) or candidate.upstream != tuple(
                accepted[parent].digest for parent in parents
            ):
                raise ValueError("STALE_UPSTREAM_LINEAGE")
        for candidate in accepted.values():
            contexts = [
                TaskContext.model_validate(event["body"]["context"])
                for event in events
                if event["kind"] == "attempt_started"
                and event["body"]["context"]["attempt_id"] == candidate.attempt_id
            ]
            if len(contexts) != 1:
                raise ValueError("UNATTESTED_CANDIDATE")
            context = contexts[0]
            if (
                context.task.digest != candidate.task_digest
                or context.authority_version != candidate.authority_version
                or tuple(item.digest for item in context.upstream) != candidate.upstream
            ):
                raise ValueError("CANDIDATE_CONTEXT_CHANGED")
            if not any(
                event["kind"] == "verification"
                and not event["body"]["goal_level"]
                and event["body"]["passed"]
                and Candidate.model_validate(event["body"]["candidate"]) == candidate
                for event in events
            ):
                raise ValueError("CANDIDATE_NOT_VERIFIED")
            self.candidates.manifest(candidate.tree)
            self._check_saved_comparison(run, context.goal, context.task, candidate)
        return accepted

    def _completion(self, run: str) -> Candidate:
        events = self.journal.events(run)
        completed = [
            event for event in events if event["kind"] == LIFECYCLE["promotion"]["precondition"]
        ]
        if not completed:
            raise ValueError("GOAL_NOT_VERIFIED")
        config = self.journal.config(run)
        goal = Goal.model_validate(
            next(event["body"]["goal"] for event in reversed(events) if event["kind"] == "goal")
        )
        if (
            goal.original_input != self.journal.original(run)
            or goal.mandatory_checks != config.mandatory_checks
        ):
            raise ValueError("GOAL_PROVENANCE_CHANGED")
        plan = Plan.model_validate(
            next(event["body"]["plan"] for event in reversed(events) if event["kind"] == "plan")
        )
        accepted = self._accepted(run, plan)
        candidate = Candidate.model_validate(completed[-1]["body"]["candidate"])
        if (
            accepted.get(plan.result_task) != candidate
            or completed[-1]["body"]["goal_digest"] != goal.digest
        ):
            raise ValueError("STALE_COMPLETION")
        if not any(
            event["kind"] == "verification"
            and event["body"]["goal_level"]
            and event["body"]["passed"]
            and Candidate.model_validate(event["body"]["candidate"]) == candidate
            for event in events
        ):
            raise ValueError("GOAL_NOT_VERIFIED")
        goal_record = next(
            e["body"]
            for e in reversed(events)
            if e["kind"] == "verification"
            and e["body"]["goal_level"]
            and e["body"]["candidate"] == candidate.model_dump(mode="json")
        )
        if "reused_from" in goal_record:
            task = next(t for t in plan.tasks if t.id == plan.result_task)
            source = self._joint_record(run, config, goal, task, candidate)
            if (
                source is None
                or digest(source) != goal_record["reused_from"]
                or source["body"]["result"] != goal_record["result"]
            ):
                raise ValueError("JOINT_VERIFICATION_CONTEXT_CHANGED")
        self._check_saved_comparison(run, goal, None, candidate)
        return candidate

    def promote(self, run: str, destination: Path) -> None:
        with self.journal.controller(run):
            self.model.reconcile(self.root / run)
            self.journal.recover_reservations(run)
            if any(
                event["kind"] in {"stopped", "uncertain", "authority_revoked"}
                for event in self.journal.events(run)
            ):
                raise ValueError("PROMOTION_AUTHORITY_UNAVAILABLE")
            candidate = self._completion(run)
            self.candidates.export_tree(candidate.tree, destination)
            self.journal.append(
                run,
                LIFECYCLE["promotion"]["postcondition"],
                candidate=candidate.digest,
                destination=str(destination.resolve()),
            )
