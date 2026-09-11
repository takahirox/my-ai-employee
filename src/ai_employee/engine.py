"""Goal clarification, graph scheduling, autonomous execution and independent verification."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import ValidationError

from .candidates import Candidates
from .capabilities import policies, readiness, validate_policy
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
from .native import Model
from .stage_contracts import VERSION, OutputViolation, StageContract, digest, validation_code

T = TypeVar("T", bound=Contract)
_NO_AUTHORITY = Authority()


class Waiting(RuntimeError):
    """Durable user input boundary, not a failure eligible for automatic retry."""


class Engine:
    def __init__(self, journal: Journal, candidates: Candidates, model: Model, root: Path) -> None:
        self.journal = journal
        self.candidates = candidates
        self.model = model
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

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
        contract = StageContract.bind(stage, prompt, config)
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
            }
        action = "output"
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
                    reason=type(error).__name__,
                )
                if authority.external_writes:
                    self.journal.append(run, "uncertain", reason="INTERRUPTED_EXTERNAL_RESPONSE")
                    raise Waiting("UNCERTAIN_EXTERNAL_EFFECT") from error
                if not policy.transport_retries:
                    raise RuntimeError("TRANSPORT_RETRY_EXHAUSTED") from error
                action = "transport"
            except OutputViolation as error:
                self.journal.append(
                    run,
                    "output_rejected",
                    stage=stage,
                    contract=contract.identity,
                    target=contract.target_digest,
                    reason=str(error),
                    authoritative=False,
                )
                if authority.external_writes:
                    self.journal.append(
                        run,
                        "uncertain",
                        reason="INVALID_EXTERNAL_RESPONSE",
                        contract=contract.identity,
                    )
                    raise Waiting("UNCERTAIN_EXTERNAL_EFFECT") from error
                feedback = {
                    "code": str(error),
                    "instruction": "Repair the response against the same contract. "
                    "Do not change accepted "
                    "criteria, policy or evaluation target. Inspect supplied files when needed.",
                }
                action = "output"
        if stage.endswith("_review"):
            raise RuntimeError("REVIEW_UNAVAILABLE")
        raise RuntimeError("OUTPUT_REPAIR_EXHAUSTED")

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
        try:
            reservation, timeout = self.journal.reserve(
                run,
                stage,
                binding=contract.projection(),
                call_key=contract.identity + ":" + action,
                call_limit=policy.revisions + 1 if action == "output" else policy.transport_retries,
            )
        except Stopped as error:
            self.journal.stop(run, str(error))
            raise
        started = time.monotonic()
        usage = Usage(tokens=0, cost=0)

        def observe(body: dict[str, Any]) -> None:
            nonlocal usage
            if body.get("event") == "usage_observed":
                usage = Usage(tokens=body.get("tokens"), cost=usage.cost)
            if body.get("event") == "usage_limit":
                self.journal.stop(run, "USAGE_LIMIT")
            self.journal.append(
                run, "worker_observation", reservation=reservation, stage=stage, observation=body
            )

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
            timeout -= time.monotonic() - started
            if timeout <= 0:
                raise TimeoutError("PREFLIGHT_TIMEOUT")
            self.journal.check(run)
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
            usage = Usage(
                tokens=returned_usage.tokens if returned_usage.tokens is not None else usage.tokens,
                cost=returned_usage.cost if returned_usage.cost is not None else usage.cost,
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
            return result
        except OutputViolation as error:
            if error.usage.tokens is not None or error.usage.cost is not None:
                usage = error.usage
            raise
        except ValidationError as error:
            raise OutputViolation(validation_code(error), usage) from None
        except (TimeoutError, ConnectionError):
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
                "weakened requirements, unsupported expansion, missing checks or evidence. "
                "Use criterion_id=review.",
                "original": original,
                "proposal": proposal.model_dump(mode="json"),
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
                    "Map exact original fragments to criteria. Preserve mandatory checks. "
                    "Before asking a question, inspect permitted original files and evidence. "
                    "Resolve factual questions (columns, values, existing files) yourself. "
                    "Only unresolved user intent, scope or authority needs a human. "
                    "Report unresolved ambiguity; do not invent authority.",
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
                ambiguous=bool(proposal.unresolved),
            )
            if accepted and proposal.unresolved:
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
                    "Check IDs refer only to available operator checks.",
                    "goal": goal.model_dump(mode="json"),
                    "feedback": feedback,
                    "available_checks": [check.model_dump(mode="json") for check in config.checks],
                },
                Plan,
                workspace,
            )
            for task in plan.tasks:
                self.journal.append(run, "readiness", **readiness(task, config))
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
            try:
                passed, evidence = self.model.check(
                    check.argv,
                    check_workspace,
                    min(check.timeout, timeout),
                    lambda: self._cancelled(run),
                )
            finally:
                self.journal.settle(
                    run, reservation, time.monotonic() - started, Usage(tokens=0, cost=0)
                )
            receipt = {
                "check": check.id,
                "passed": passed,
                "evidence": evidence,
                "candidate": candidate.digest,
            }
            receipts.append(receipt)
            self.journal.append(run, "check_result", **receipt)
        events = self.journal.events(run)
        external = any(
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
        feedback = ""
        passed = False
        for _ in range(config.verification.revisions + 1):
            verification = self._generate(
                run,
                "goal_verification" if task is None else "task_verification",
                config.verification,
                {
                    "instruction": "Independently verify actual candidate files against EACH exact "
                    "criterion and required evidence. Worker claims alone are not proof. "
                    "Checks prove only their named coverage. Missing evidence fails. "
                    "Never repeat an external side effect to verify it.",
                    "goal": goal.model_dump(mode="json"),
                    "task": None if task is None else task.model_dump(mode="json"),
                    "criteria": [item.model_dump(mode="json") for item in criteria],
                    "candidate": candidate.model_dump(mode="json"),
                    "checks": receipts,
                    "upstream_check_evidence": prior_checks,
                    "review_feedback": feedback,
                    "worker_result": None if result is None else result.model_dump(mode="json"),
                },
                Verification,
                self._workspace(run, "verification", candidate.tree),
            )
            reviewed, feedback = self._review(
                run,
                config.verification,
                "verification",
                verification,
                {
                    "criteria": [item.model_dump(mode="json") for item in criteria],
                    "candidate": candidate.model_dump(mode="json"),
                    "checks": receipts,
                },
                self._workspace(run, "verification-review", candidate.tree),
                external=external,
            )
            if reviewed:
                passed = verification.accepts(criteria) and all(item["passed"] for item in receipts)
                # Generic TLS observation cannot attest remote operation semantics.
                # Without a real service read provider, require an operator-owned
                # evidence check (e.g. signed receipt validation), never only prose.
                if external and not (receipts or prior_checks):
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
            goal_level=task is None,
            passed=passed,
            result=verification.model_dump(mode="json"),
        )
        return passed

    def _authorize(
        self, run: str, config: RunConfig, authority: Authority, task_digest: str
    ) -> None:
        reason = None
        if not authority.within(config.authority_ceiling):
            reason = "AUTHORITY_EXCEEDS_POLICY"
        elif (
            config.security == "strict"
            and authority.external_writes
            and not (authority.operation_approval and authority.duplicate_prevention)
        ):
            reason = "STRICT_OPERATION_BOUNDARY_REQUIRED"
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
            if selection.index >= len(config.worker_options):
                feedback = "WORKER_SELECTION_OUT_OF_RANGE"
                continue
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
        if not results or results[-1].status != "completed":
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
        else:
            reviewed, _ = self._review(
                run,
                policy,
                "worker",
                results[-1],
                {
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
                        "accepted",
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
        for _ in range(len(prior), config.limits.task_attempts):
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
                    },
                    WorkerResult,
                    workspace,
                    authority,
                )
                if result.status == "usage_limit":
                    self.journal.stop(run, "USAGE_LIMIT")
                    raise Stopped("USAGE_LIMIT")
                if result.status != "authority_requested":
                    self.journal.append(
                        run, "worker_result", attempt=attempt, result=result.model_dump(mode="json")
                    )
                if result.status == "uncertain":
                    self.journal.append(
                        run,
                        "uncertain",
                        task_digest=task.digest,
                        attempt=attempt,
                        result=result.model_dump(mode="json"),
                    )
                    raise Waiting("UNCERTAIN_EXTERNAL_EFFECT")
                if result.status == "authority_requested":
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
                if result.status != "completed":
                    if authority.external_writes:
                        self.journal.append(
                            run,
                            "uncertain",
                            task_digest=task.digest,
                            attempt=attempt,
                            reason="EXTERNAL_WORKER_FAILED",
                        )
                        raise Waiting("UNCERTAIN_EXTERNAL_EFFECT")
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
                        "task": task.model_dump(mode="json"),
                        "candidate": candidate.model_dump(mode="json"),
                    },
                    self._workspace(run, "worker-review", candidate.tree),
                    external=authority.external_writes,
                )
                if reviewed and self._verify(run, config, goal, task, candidate, result):
                    self.journal.append(
                        run, "accepted", task=task.id, candidate=candidate.model_dump(mode="json")
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
        raise ValueError("TASK_ATTEMPTS_EXHAUSTED")

    def execute(self, run: str) -> None:
        with self.journal.controller(run):
            try:
                self.model.reconcile(self.root / run)
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
        plan = (
            Plan.model_validate(plans[-1]["body"]["plan"])
            if plans
            else self._plan(run, goal, config, base)
        )
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
        if not self._verify(run, config, goal, None, final, None):
            self.journal.append(run, "goal_rejected", candidate=final.model_dump(mode="json"))
            if any(task.authority.external_writes for task in plan.tasks):
                self.journal.append(run, "uncertain", reason="EXTERNAL_GOAL_UNVERIFIED")
                raise Waiting("UNVERIFIED_EXTERNAL_EFFECT")
            self._recover(run, config, goal, plan, accepted, "GOAL_VERIFICATION_FAILED", final.tree)
            return self._execute(run)
        self.journal.check(run)
        self.journal.append(
            run, "completed", candidate=final.model_dump(mode="json"), goal_digest=goal.digest
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
        feedback = ""
        for _ in range(config.recovery.revisions + 1):
            extension = self._generate(
                run,
                "recovery",
                config.recovery,
                {
                    "instruction": "Extend the adopted graph forward to repair the failure. "
                    "Return the full next adopted Plan. Existing task IDs must retain "
                    "their exact definitions; introduce new IDs for repairs/reintegration. "
                    "Reuse accepted upstream tasks, preserve their required ancestors, "
                    "and remove superseded future paths from the adopted plan. "
                    "Never weaken Goal Success Criteria. Repair tasks can depend on "
                    "accepted candidates; do not depend on permanently failed tasks.",
                    "goal": goal.model_dump(mode="json"),
                    "previous_plan": previous.model_dump(mode="json"),
                    "historical_tasks": [
                        t.model_dump(mode="json")
                        for t in {t.id: t for p in plans for t in p.tasks}.values()
                    ],
                    "available_checks": [c.model_dump(mode="json") for c in config.checks],
                    "accepted": {
                        key: value.model_dump(mode="json") for key, value in accepted.items()
                    },
                    "failure": failure,
                    "evidence": evidence,
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
                {
                    "goal": goal.model_dump(mode="json"),
                    "previous_plan": previous.model_dump(mode="json"),
                    "failure": failure,
                },
                workspace,
                external=any(task.authority.external_writes for task in extension.tasks),
            )
            if reviewed:
                break
        else:
            raise ValueError("RECOVERY_REJECTED")
        historical = {task.id: task for plan in plans for task in plan.tasks}
        new = {task.id for task in extension.tasks} - historical.keys()
        original = {task.id for task in plans[0].tasks}
        if not new or len((historical.keys() | new) - original) > config.limits.added_tasks:
            raise ValueError("GRAPH_GROWTH_LIMIT")
        for task in extension.tasks:
            if task.id in historical and task.digest != historical[task.id].digest:
                raise ValueError("HISTORICAL_TASK_REWRITTEN")
            if task.supersedes is not None and task.supersedes not in historical:
                raise ValueError("FOREIGN_REPAIR_TARGET")
            self.journal.append(run, "readiness", **readiness(task, config))
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
        return accepted

    def _completion(self, run: str) -> Candidate:
        events = self.journal.events(run)
        completed = [event for event in events if event["kind"] == "completed"]
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
        return candidate

    def promote(self, run: str, destination: Path) -> None:
        with self.journal.controller(run):
            self.model.reconcile(self.root / run)
            if any(
                event["kind"] in {"stopped", "uncertain", "authority_revoked"}
                for event in self.journal.events(run)
            ):
                raise ValueError("PROMOTION_AUTHORITY_UNAVAILABLE")
            candidate = self._completion(run)
            self.candidates.publish_directory(candidate, destination)
            self.journal.append(
                run, "promoted", candidate=candidate.digest, destination=str(destination.resolve())
            )
