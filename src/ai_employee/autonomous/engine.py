"""Goal clarification, graph scheduling, autonomous execution and independent verification."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from .candidates import Candidates
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
        self.root.mkdir(parents=True, exist_ok=True)

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
        reservation, timeout = self.journal.reserve(run, stage)
        started = time.monotonic()
        usage = Usage()
        try:
            result, usage = self.model.generate(
                policy,
                json.dumps(prompt, ensure_ascii=False),
                schema,
                workspace,
                authority,
                timeout,
                lambda: self._cancelled(run),
            )
            self.journal.check(run)
            self.journal.append(
                run,
                "stage_result",
                stage=stage,
                reservation=reservation,
                result_digest=result.digest,
                backend=policy.backend,
                model=policy.model,
                effort=policy.effort,
            )
            return result
        except Stopped as error:
            self.journal.stop(run, str(error))
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
        reviewer = policy.model_copy(update={"model": policy.reviewer_model or policy.model})
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
        return review.accepts(
            (Criterion(id="review", description="proposal is acceptable"),)
        ), review.summary

    def start(self, original: str, config: RunConfig, source: Path) -> str:
        run = self.prepare(original, config, source)
        self.execute(run)
        return run

    def prepare(self, original: str, config: RunConfig, source: Path) -> str:
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
            if proposal.unresolved:
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
                self._check_ids(task.criteria, config)
                if not task.authority.within(config.authority_ceiling):
                    raise ValueError("PLAN_AUTHORITY_EXCEEDS_POLICY")
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
        workspace = self._workspace(run, "verification", candidate.tree)
        check_ids = (set(goal.mandatory_checks) if task is None else set()) | {
            key for item in criteria for key in item.checks
        }
        receipts: list[dict[str, Any]] = []
        for check in config.checks:
            if check.id not in check_ids:
                continue
            check_workspace = self._workspace(run, "check", candidate.tree)
            reservation, timeout = self.journal.reserve(run, "check")
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
        verification = self._generate(
            run,
            "goal_verification" if task is None else "task_verification",
            config.verification,
            {
                "instruction": "Independently verify the actual candidate files against EACH exact "
                "criterion and required evidence. Worker claims alone are not proof. "
                "Checks prove only their named coverage. Missing evidence fails. "
                "Never repeat an external side effect to verify it.",
                "goal": goal.model_dump(mode="json"),
                "task": None if task is None else task.model_dump(mode="json"),
                "criteria": [item.model_dump(mode="json") for item in criteria],
                "candidate": candidate.model_dump(mode="json"),
                "checks": receipts,
                "worker_result": None if result is None else result.model_dump(mode="json"),
            },
            Verification,
            workspace,
        )
        passed = verification.accepts(criteria) and all(item["passed"] for item in receipts)
        reviewed, _ = self._review(
            run,
            config.verification,
            "verification",
            verification,
            {
                "criteria": [item.model_dump(mode="json") for item in criteria],
                "candidate": candidate.model_dump(mode="json"),
                "checks": receipts,
            },
            workspace,
        )
        passed = passed and reviewed
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

    def _execute_task(
        self,
        run: str,
        task: Task,
        goal: Goal,
        config: RunConfig,
        upstream: tuple[Candidate, ...],
        base: str,
    ) -> Candidate:
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
            selector = config.selection or config.planning
            selection = self._generate(
                run,
                "selection",
                selector,
                {
                    "instruction": "Select the best worker option for this Task. "
                    "Return its zero-based index.",
                    "task": task.model_dump(mode="json"),
                    "options": [policy.model_dump(mode="json") for policy in config.worker_options],
                },
                WorkerChoice,
                self._workspace(run, "selection"),
            )
            if selection.index >= len(config.worker_options):
                raise ValueError("WORKER_SELECTION_OUT_OF_RANGE")
            worker_policy = config.worker_options[selection.index]
        if not choices:
            self.journal.append(
                run,
                "worker_selected",
                task_digest=task.digest,
                policy=worker_policy.model_dump(mode="json"),
            )
        authority = task.authority
        if not authority.within(config.authority_ceiling):
            raise ValueError("AUTHORITY_EXCEEDS_POLICY")
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
        if prior:
            previous = TaskContext.model_validate(prior[-1]["body"]["context"])
            workspace = Path(previous.workspace)
            if not workspace.is_relative_to(self.root / run) or not workspace.is_dir():
                raise ValueError("WORKSPACE_UNAVAILABLE")
            if not applied:
                authority, version = previous.authority, previous.authority_version
        else:
            workspace = self._workspace(
                run, "worker", upstream[0].tree if len(upstream) == 1 else base
            )
        if len(upstream) > 1 and not prior:
            inputs = workspace / ".fleet-inputs"
            for index, candidate in enumerate(upstream):
                self.candidates.materialize(candidate.tree, inputs / str(index))
        for _ in range(len(prior), config.limits.task_attempts):
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
                    self.journal.append(
                        run,
                        "authority_requested",
                        attempt=attempt,
                        requested=requested.model_dump(mode="json"),
                        version=version + 1,
                    )
                    self.journal.append(
                        run,
                        "approval_wait",
                        attempt=attempt,
                        workspace=str(workspace),
                        task_digest=task.digest,
                    )
                    raise Waiting("PAUSED_FOR_APPROVAL")
                if result.status != "completed":
                    feedback = result.summary
                    self.journal.append(
                        run, "attempt_failed", attempt=attempt, reason="WORKER_FAILED"
                    )
                    continue
                candidate = self.candidates.freeze(context)
                self.journal.append(run, "candidate", candidate=candidate.model_dump(mode="json"))
                if self._verify(run, config, goal, task, candidate, result):
                    self.journal.append(
                        run, "accepted", task=task.id, candidate=candidate.model_dump(mode="json")
                    )
                    return candidate
                feedback = (
                    "Independent verification rejected this candidate. "
                    "Inspect verification evidence and repair."
                )
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
        raise ValueError("TASK_ATTEMPTS_EXHAUSTED")

    def execute(self, run: str) -> None:
        with self.journal.controller(run):
            try:
                self._execute(run)
            except Waiting:
                return
            except Stopped as error:
                self.journal.stop(run, str(error))
                raise
            except (ValueError, TimeoutError) as error:
                self.journal.append(run, "failed", reason=str(error)[:200])
                raise

    def answer(self, run: str, answer: str) -> None:
        """Record explicit clarification input without changing original provenance."""
        if not answer.strip():
            raise ValueError("EMPTY_CLARIFICATION_ANSWER")
        with self.journal.controller(run):
            if not any(event["kind"] == "clarification_wait" for event in self.journal.events(run)):
                raise ValueError("NO_PENDING_CLARIFICATION")
            self.journal.append(run, "clarification_answer", answer=answer)

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
            if not authority.within(self.journal.config(run).authority_ceiling):
                raise ValueError("AUTHORITY_EXCEEDS_POLICY")
            workspace = Path(wait["workspace"])
            if not workspace.resolve().is_relative_to(self.root / run):
                raise ValueError("FOREIGN_WORKSPACE")
            self.journal.append(
                run, "authority_approved", attempt=attempt, version=request["version"]
            )
            # No live Worker exists at this boundary. New native sessions use
            # confirmed policy and the retained workspace on continuation.
            self.model.apply_authority(workspace, authority)
            self.journal.append(
                run,
                "authority_applied",
                attempt=attempt,
                task_digest=wait["task_digest"],
                version=request["version"],
                authority=authority.model_dump(mode="json"),
            )

    def _execute(self, run: str) -> None:
        self.journal.check(run)
        config = self.journal.config(run)
        events = self.journal.events(run)
        if any(event["kind"] == "completed" for event in events):
            return
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
        plans = [event for event in events if event["kind"] == "plan"]
        plan = (
            Plan.model_validate(plans[-1]["body"]["plan"])
            if plans
            else self._plan(run, goal, config, base)
        )
        accepted: dict[str, Candidate] = {}
        tasks = {task.id: task for task in plan.tasks}
        for event in self.journal.events(run):
            if event["kind"] != "accepted":
                continue
            candidate = Candidate.model_validate(event["body"]["candidate"])
            key = event["body"]["task"]
            if key in tasks and candidate.task_digest == tasks[key].digest:
                accepted[key] = candidate
        for key, candidate in accepted.items():
            if candidate.upstream != tuple(
                accepted[parent].digest for parent in tasks[key].dependencies
            ):
                raise ValueError("STALE_UPSTREAM_LINEAGE")
            self.candidates.manifest(candidate.tree)
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
                for future in as_completed(futures):
                    key = futures[future]
                    try:
                        accepted[key] = future.result()
                        pending.remove(key)
                    except (ValueError, TimeoutError) as error:
                        failure = str(error)
                        self.journal.append(run, "task_failed", task=key, reason=failure[:200])
            if failure is not None:
                self._recover(run, config, goal, plan, accepted, failure, base)
                return self._execute(run)
        final = accepted[plan.result_task]
        if not self._verify(run, config, goal, None, final, None):
            self.journal.append(run, "goal_rejected", candidate=final.model_dump(mode="json"))
            self._recover(run, config, goal, plan, accepted, "GOAL_VERIFICATION_FAILED", final.tree)
            return self._execute(run)
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
                "accepted": {key: value.model_dump(mode="json") for key, value in accepted.items()},
                "failure": failure,
                "evidence": evidence,
            },
            Plan,
            workspace,
        )
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
            self._check_ids(task.criteria, config)
            if not task.authority.within(config.authority_ceiling):
                raise ValueError("RECOVERY_AUTHORITY_EXCEEDS_POLICY")
        self.journal.append(
            run,
            "plan",
            plan=extension.model_dump(mode="json"),
            previous=previous.digest,
            reason=failure,
            added=sorted(new),
        )

    def promote(self, run: str, destination: Path) -> None:
        with self.journal.controller(run):
            events = self.journal.events(run)
            completed = [event for event in events if event["kind"] == "completed"]
            if not completed:
                raise ValueError("GOAL_NOT_VERIFIED")
            candidate = Candidate.model_validate(completed[-1]["body"]["candidate"])
            self.journal.config(run)
            self.candidates.publish_directory(candidate, destination)
            self.journal.append(
                run, "promoted", candidate=candidate.digest, destination=str(destination.resolve())
            )
