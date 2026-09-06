"""Private, explicit reconstruction of task inputs, never historical worker answers."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import Field, model_validator

from .config import OperatorConfig, load_operator_config
from .domain import ExecutionPolicy, Goal, ProjectHarnessV2
from .domain.base import Digest
from .domain.policy_v2 import PolicyLayer
from .domain.v2 import DigestedRecordV2, SchemaModelV2, WorkspaceSnapshot
from .goal_acceptance import harness_for_goal
from .project import PROFILE_PATHS, _derive_required_process_evaluators, discover_project_harness
from .serialization import (
    canonical_digest,
    canonical_json,
    loads_yaml_model,
    operator_config_digest,
    project_harness_digest,
)
from .services_v2._common import now
from .storage import SQLiteStore
from .task_orchestration import GraphRunRecord

EMPTY_STATUS = hashlib.sha256(b"").hexdigest()
GIT_ENVIRONMENT = {
    "PATH": os.defpath,
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}


class HistoricalStart(DigestedRecordV2):
    schema_name: ClassVar[str] = "historical_start"
    repository: str
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    goal: Goal
    harness: ProjectHarnessV2
    operator_config: OperatorConfig
    policy: PolicyLayer
    dirty_state_digest: Digest
    runtime_source_digest: Digest | None = None
    origin: Literal["captured_before_models", "recovered_exact_records"]


class ExternalFixture(SchemaModelV2):
    schema_name: ClassVar[str] = "historical_external_fixture"
    path: str = Field(min_length=1, max_length=4_096)
    sha256: Digest


class CorpusEnvironment(SchemaModelV2):
    """Operator attestation, not an inferred reconstruction of an unknown environment."""

    schema_name: ClassVar[str] = "corpus_environment"
    identity: str = Field(min_length=1, max_length=1_000)
    external_files: tuple[ExternalFixture, ...]
    complete: bool
    uncommitted_state_required: bool
    declaration: Literal["operator_attested_complete_task_environment"]


class HistoricalTask(DigestedRecordV2):
    schema_name: ClassVar[str] = "historical_evaluation_task"
    source_run_digest: Digest
    logical_task_digest: Digest
    start: HistoricalStart
    execution_policy: ExecutionPolicy
    environment: CorpusEnvironment
    task_class: str = Field(min_length=1, max_length=100)
    historical_status: str
    historical_replans: int = Field(ge=0)
    privacy: Literal["local_private"] = "local_private"
    planning: Literal["fresh_from_original_goal"] = "fresh_from_original_goal"

    @model_validator(mode="after")
    def _exact_start(self) -> HistoricalTask:
        if self.start.run_id != self.run_id or self.start.dirty_state_digest != EMPTY_STATUS:
            raise ValueError("task must bind a clean historical start for this run")
        if not self.environment.complete or self.environment.uncommitted_state_required:
            raise ValueError("task environment is incomplete")
        if self.logical_task_digest != logical_task_digest(self.start):
            raise ValueError("logical task identity is stale")
        return self


class CorpusTrialBinding(DigestedRecordV2):
    schema_name: ClassVar[str] = "corpus_trial_binding"
    fixture_digest: Digest
    logical_task_digest: Digest
    environment_digest: Digest
    execution_profile_digest: Digest


class CorpusCandidate(SchemaModelV2):
    schema_name: ClassVar[str] = "corpus_candidate"
    run_id: str
    classification: Literal[
        "REPRODUCIBLE",
        "BASE_COMMIT_UNAVAILABLE",
        "REPOSITORY_UNAVAILABLE",
        "HARNESS_UNAVAILABLE",
        "CONFIG_UNAVAILABLE",
        "CONFIG_DIGEST_MISMATCH",
        "EXTERNAL_FIXTURE_MISSING",
        "UNCOMMITTED_START_STATE",
        "DUPLICATE_TASK",
        "INSUFFICIENT_PROVENANCE",
    ]
    reason: str
    task: HistoricalTask | None = None
    duplicate_of: str | None = None


class CorpusUnavailable(ValueError):
    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        super().__init__(reason)


def _git(repository: Path, *args: str) -> bytes:
    result = subprocess.run(
        (
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "--no-optional-locks",
            "-C",
            str(repository),
            *args,
        ),
        capture_output=True,
        check=False,
        timeout=30,
        env=GIT_ENVIRONMENT,
    )
    if result.returncode:
        raise ValueError("local Git provenance check failed")
    return result.stdout


def capture_start(
    store: SQLiteStore,
    run_id: str,
    repository: Path,
    goal: Goal,
    harness: ProjectHarnessV2,
    operator: OperatorConfig,
    policy: PolicyLayer,
) -> None:
    """Record configuration bodies without reading auth, outputs or ignored file contents."""
    if harness.provisional:
        return
    record = HistoricalStart(
        id="start-" + run_id,
        run_id=run_id,
        created_at=now(),
        repository=str(repository),
        base_commit=_git(repository, "rev-parse", "HEAD").decode().strip(),
        goal=goal,
        harness=harness,
        operator_config=operator,
        policy=policy,
        dirty_state_digest=hashlib.sha256(
            _git(repository, "status", "--porcelain=v2", "--untracked-files=all")
        ).hexdigest(),
        runtime_source_digest=canonical_digest(
            tuple(
                (
                    str(path.relative_to(Path(__file__).parent)),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
                for path in sorted(Path(__file__).parent.rglob("*.py"))
                if not path.is_symlink()
            )
        ),
        origin="captured_before_models",
    )
    store.put_once("historical_start_v2", record, run_id=run_id)


def logical_task_digest(start: HistoricalStart) -> str:
    return canonical_digest(
        (
            str(Path(start.repository).resolve()),
            start.base_commit,
            start.goal.model_dump(mode="python", exclude={"id"}),
        )
    )


def _base_harness(repository: Path, commit: str, goal: Goal) -> ProjectHarnessV2:
    for relative in PROFILE_PATHS:
        try:
            body = _git(repository, "show", f"{commit}:{relative}").decode()
        except ValueError:
            continue
        try:
            harness = _derive_required_process_evaluators(loads_yaml_model(body, ProjectHarnessV2))
            return harness_for_goal(harness, goal)
        except ValueError as error:
            raise CorpusUnavailable(
                "HARNESS_UNAVAILABLE", "A recoverable v2 Harness is required."
            ) from error
    raise CorpusUnavailable(
        "HARNESS_UNAVAILABLE", "No explicit v2 Harness exists at the recorded base."
    )


def _recover_start(store: SQLiteStore, run: GraphRunRecord) -> HistoricalStart:
    try:
        return store.get("historical_start_v2", "start-" + run.id, HistoricalStart)
    except KeyError:
        pass
    if run.repository is None or run.base_commit is None:
        raise CorpusUnavailable(
            "INSUFFICIENT_PROVENANCE", "Repository/base provenance was not recorded."
        )
    # Scope proof to exact child requests; another task's clean snapshot is not evidence.
    from .domain.v2 import WorkerRequest

    children = {
        item.run_id
        for item in store.list_records("worker_request_v2", WorkerRequest)
        if item.graph_run_id == run.id
    }
    snapshots = tuple(
        item
        for item in store.list_records("workspace_v2", WorkspaceSnapshot)
        if item.run_id in children
        and item.original_worktree == run.repository
        and item.head_commit == run.base_commit
    )
    if not snapshots:
        raise CorpusUnavailable(
            "INSUFFICIENT_PROVENANCE", "No task-bound clean start snapshot was retained."
        )
    if any(item.dirty_state_digest != EMPTY_STATUS for item in snapshots):
        raise CorpusUnavailable(
            "UNCOMMITTED_START_STATE", "Historical start depended on uncommitted state."
        )
    harness = _base_harness(Path(run.repository), run.base_commit, run.goal)
    if run.operator_config_path is None or not Path(run.operator_config_path).is_file():
        raise CorpusUnavailable(
            "CONFIG_UNAVAILABLE", "Historical operator config body is unavailable."
        )
    operator = load_operator_config(run.operator_config_path)
    policies = tuple(
        item
        for item in store.list_records("policy_layer_v2", PolicyLayer)
        if item.run_id == run.id
        and canonical_digest((item.content_digest,)) == run.effective_policy_digest
    )
    if len(policies) != 1:
        raise CorpusUnavailable(
            "INSUFFICIENT_PROVENANCE", "Exact policy content is missing or ambiguous."
        )
    return HistoricalStart(
        id="start-" + run.id,
        run_id=run.id,
        created_at=now(),
        repository=run.repository,
        base_commit=run.base_commit,
        goal=run.goal,
        harness=harness,
        operator_config=operator,
        policy=policies[0],
        dirty_state_digest=EMPTY_STATUS,
        origin="recovered_exact_records",
    )


def verify_environment(environment: CorpusEnvironment) -> None:
    if environment.uncommitted_state_required:
        raise CorpusUnavailable(
            "UNCOMMITTED_START_STATE", "Environment requires unavailable uncommitted inputs."
        )
    if not environment.complete:
        raise CorpusUnavailable(
            "EXTERNAL_FIXTURE_MISSING", "Operator has not attested a complete environment."
        )
    for fixture in environment.external_files:
        path = Path(fixture.path)
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise CorpusUnavailable(
                "EXTERNAL_FIXTURE_MISSING", "An attested external fixture is unavailable."
            )
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != fixture.sha256:
            raise CorpusUnavailable(
                "EXTERNAL_FIXTURE_MISSING", "An external fixture fingerprint changed."
            )


def inspect_candidate(
    store: SQLiteStore,
    run: GraphRunRecord,
    environment: CorpusEnvironment | None,
    *,
    task_class: str = "unclassified",
) -> CorpusCandidate:
    try:
        if run.repository is None or not Path(run.repository).is_dir():
            raise CorpusUnavailable(
                "REPOSITORY_UNAVAILABLE", "Resolve the original repository locally."
            )
        repository = Path(run.repository)
        try:
            if (
                run.base_commit is None
                or _git(repository, "rev-parse", f"{run.base_commit}^{{commit}}").decode().strip()
                != run.base_commit
            ):
                raise ValueError("base missing")
        except ValueError as error:
            raise CorpusUnavailable(
                "BASE_COMMIT_UNAVAILABLE", "Restore the exact historical base commit locally."
            ) from error
        start = _recover_start(store, run)
        if start.dirty_state_digest != EMPTY_STATUS:
            raise CorpusUnavailable(
                "UNCOMMITTED_START_STATE", "Historical source was not clean at capture."
            )
        if (
            start.goal != run.goal
            or start.repository != run.repository
            or start.base_commit != run.base_commit
            or project_harness_digest(start.harness) != run.harness_digest
            or operator_config_digest(start.operator_config) != run.operator_config_digest
            or canonical_digest((start.policy.content_digest,)) != run.effective_policy_digest
        ):
            raise CorpusUnavailable(
                "CONFIG_DIGEST_MISMATCH",
                "Recovered input does not match authoritative run digests.",
            )
        if (
            project_harness_digest(_base_harness(repository, run.base_commit, run.goal))
            != run.harness_digest
        ):
            raise CorpusUnavailable(
                "CONFIG_DIGEST_MISMATCH",
                "Base-commit Harness cannot restore the recorded requirements.",
            )
        if not run.goal.completion_criteria and run.goal.task_kind.value == "mutating":
            raise CorpusUnavailable(
                "INSUFFICIENT_PROVENANCE", "Mutating task has no recorded acceptance criteria."
            )
        if environment is None:
            raise CorpusUnavailable(
                "EXTERNAL_FIXTURE_MISSING", "Supply a complete operator-attested task environment."
            )
        verify_environment(environment)
        task = HistoricalTask(
            id="history-task-" + run.id,
            run_id=run.id,
            created_at=now(),
            source_run_digest=canonical_digest(run),
            logical_task_digest=logical_task_digest(start),
            start=start,
            execution_policy=run.execution_policy,
            environment=environment,
            task_class=task_class,
            historical_status=run.status,
            historical_replans=run.replan_count,
        )
        return CorpusCandidate(
            run_id=run.id,
            classification="REPRODUCIBLE",
            reason="Exact local inputs recovered; environment is operator-attested.",
            task=task,
        )
    except CorpusUnavailable as error:
        return CorpusCandidate.model_validate(
            {"run_id": run.id, "classification": error.code, "reason": str(error)}
        )
    except (KeyError, ValueError, OSError, subprocess.TimeoutExpired):
        return CorpusCandidate(
            run_id=run.id,
            classification="INSUFFICIENT_PROVENANCE",
            reason="Historical data failed strict provenance validation.",
        )


def inspect_corpus(
    store: SQLiteStore,
    environment: CorpusEnvironment | None,
    *,
    run_ids: tuple[str, ...] = (),
    task_class: str = "unclassified",
) -> tuple[CorpusCandidate, ...]:
    # A corrupt candidate must get its own rejection, not hide every other History task.
    latest: dict[str, GraphRunRecord | None] = {}
    for row in store._connection.execute(
        "SELECT record_id,payload FROM records WHERE kind='graph_run_v2' "
        "ORDER BY record_id,revision"
    ):
        try:
            decoded_run = GraphRunRecord.model_validate_json(row["payload"])
            latest[row["record_id"]] = decoded_run if decoded_run.id == row["record_id"] else None
        except ValueError:
            latest[row["record_id"]] = None
    seen: dict[str, str] = {}
    results = []
    registered_ids = {
        str(item["run_id"])
        for item in store.list_run_repositories()
        if isinstance(item.get("run_id"), str)
    }
    for run_id in run_ids or tuple(sorted(set(latest) | registered_ids)):
        run = latest.get(run_id)
        if run is None:
            results.append(
                CorpusCandidate(
                    run_id=run_id,
                    classification="INSUFFICIENT_PROVENANCE",
                    reason="No authoritative Graph Run exists for this selection.",
                )
            )
            continue
        candidate = inspect_candidate(store, run, environment, task_class=task_class)
        if candidate.task is not None:
            logical = candidate.task.logical_task_digest
            if logical in seen:
                candidate = CorpusCandidate(
                    run_id=run_id,
                    classification="DUPLICATE_TASK",
                    reason="Same original Goal, repository and baseline as another selected run.",
                    duplicate_of=seen[logical],
                )
            else:
                seen[logical] = run_id
        results.append(candidate)
    return tuple(results)


def write_private(path: Path, value: object) -> None:
    """Never overwrite or follow an output symlink; exporting content is explicit."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(canonical_json(value) + "\n")


def load_task(path: Path) -> HistoricalTask:
    if path.stat().st_size > 2_000_000:
        raise ValueError("fixture exceeds the supported private input bound")
    try:
        return HistoricalTask.model_validate_json(path.read_bytes())
    except ValueError:
        raise ValueError("fixture failed strict schema/content_digest validation") from None


def restore_task(task: HistoricalTask, destination: Path) -> Path:
    verify_environment(task.environment)
    if destination.exists() or destination.is_symlink():
        raise ValueError("restore destination must be absent")
    repository = Path(task.start.repository).resolve()
    if not repository.is_dir():
        raise ValueError("original local repository is unavailable")
    if project_harness_digest(
        _base_harness(repository, task.start.base_commit, task.start.goal)
    ) != project_harness_digest(task.start.harness):
        raise ValueError("fixture Harness cannot be reconstructed from the recorded base")
    destination.mkdir(mode=0o700)
    result = subprocess.run(
        (
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "clone",
            "--no-checkout",
            "--no-hardlinks",
            "--",
            str(repository),
            str(destination),
        ),
        capture_output=True,
        check=False,
        timeout=60,
        env=GIT_ENVIRONMENT,
    )
    if result.returncode:
        raise ValueError("private local clone failed; partial destination was retained")
    _git(destination, "checkout", "--detach", task.start.base_commit)
    harness = harness_for_goal(discover_project_harness(destination), task.start.goal)
    if project_harness_digest(harness) != project_harness_digest(task.start.harness):
        raise ValueError("restored Harness differs; destination retained for inspection")
    if _git(destination, "status", "--porcelain=v2", "--untracked-files=all"):
        raise ValueError("restored fixture is not a clean baseline")
    return destination
