"""Typed Project Harness v2 and restrictive v1 conversion contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import ClassVar, Literal, Self

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator
from pydantic.main import BaseModel

from .base import FrozenDict, StableStrEnum
from .browser import BROWSER_EVALUATOR_ID, BrowserScenario
from .evaluation import AVAILABLE_FIRST_PARTY_EVALUATOR_IDS, RESERVED_EVALUATOR_IDS
from .models import ProjectProfile, ProvenancedValue


class HarnessModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        use_enum_values=False,
        arbitrary_types_allowed=True,
    )
    schema_name: ClassVar[str]


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or value.startswith("/") or "\\" in value or ".." in path.parts:
        raise ValueError("Harness paths must be workspace-relative POSIX paths")
    if value != "." and path.as_posix() != value:
        raise ValueError("Harness paths must use canonical workspace-relative POSIX syntax")
    return value


class HarnessCommand(HarnessModel):
    schema_name: ClassVar[str] = "harness_command"
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str = "."
    inherit_environment: tuple[str, ...] = ()

    _cwd_is_relative = field_validator("cwd")(_safe_relative)

    @field_validator("argv")
    @classmethod
    def _nonempty_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("command entries must be non-empty and NUL-free")
        return value

    @field_validator("inherit_environment")
    @classmethod
    def _unique_environment(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("environment names must be non-empty and NUL-free")
        if len(value) != len(set(value)):
            raise ValueError("environment names must be unique")
        return value


class HarnessPaths(HarnessModel):
    schema_name: ClassVar[str] = "harness_paths"
    writable: tuple[str, ...] = ()
    protected: tuple[str, ...] = (".git/**",)
    generated: tuple[str, ...] = ()

    @field_validator("writable", "protected", "generated")
    @classmethod
    def _relative_unique_globs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("path lists must not contain duplicates")
        return tuple(_safe_relative(item) for item in value)

    @model_validator(mode="after")
    def _no_writable_protected_conflict(self) -> Self:
        conflict = {
            f"{writable} <-> {protected}"
            for writable in self.writable
            for protected in self.protected
            if _globs_may_overlap(writable, protected)
        }
        if conflict:
            raise ValueError(f"paths cannot be writable and protected: {sorted(conflict)}")
        return self


def _glob_prefix(value: str) -> tuple[str, ...]:
    """Return path components before the first glob metacharacter."""

    result: list[str] = []
    for part in PurePosixPath(value).parts:
        if any(character in part for character in "*?["):
            break
        result.append(part)
    return tuple(result)


def _globs_may_overlap(left: str, right: str) -> bool:
    """Reject equal, literal-matching, and recursive-glob path overlaps."""

    if left == right:
        return True
    left_is_glob = any(character in left for character in "*?[")
    right_is_glob = any(character in right for character in "*?[")
    if not right_is_glob and PurePosixPath(right).match(left):
        return True
    if not left_is_glob and PurePosixPath(left).match(right):
        return True
    left_prefix = _glob_prefix(left)
    right_prefix = _glob_prefix(right)
    if not left_prefix or not right_prefix or ("**" not in left and "**" not in right):
        return False
    shared = min(len(left_prefix), len(right_prefix))
    return left_prefix[:shared] == right_prefix[:shared]


class HarnessReview(HarnessModel):
    schema_name: ClassVar[str] = "harness_review"
    required: bool = True
    independent_task_review: bool = False
    parent_semantic_review: bool = False
    block_severities: tuple[Literal["critical", "high", "medium", "low"], ...] = (
        "critical",
        "high",
    )

    @model_validator(mode="after")
    def _review_policy_is_canonical(self) -> Self:
        if len(self.block_severities) != len(set(self.block_severities)):
            raise ValueError("review blocking severities must be unique")
        if (self.independent_task_review or self.parent_semantic_review) and not self.required:
            raise ValueError("AI review requires the review gate")
        if self.parent_semantic_review and not self.block_severities:
            raise ValueError("parent semantic review requires blocking severities")
        return self


class HarnessEvaluator(HarnessModel):
    schema_name: ClassVar[str] = "harness_evaluator"
    id: str = Field(min_length=1, max_length=200)
    provider_id: str = Field(min_length=1, max_length=200)
    command_ref: str | None = Field(default=None, min_length=1, max_length=200)
    browser_scenario: BrowserScenario | None = None
    criterion_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _provider_and_criteria_are_consistent(self) -> Self:
        if len(self.criterion_ids) != len(set(self.criterion_ids)):
            raise ValueError("evaluator criterion IDs must be unique")
        if any(not value.strip() for value in self.criterion_ids):
            raise ValueError("evaluator criterion IDs must be non-blank")
        if self.provider_id in RESERVED_EVALUATOR_IDS:
            raise ValueError(f"evaluator provider is reserved but unavailable: {self.provider_id}")
        if self.provider_id not in AVAILABLE_FIRST_PARTY_EVALUATOR_IDS:
            raise ValueError(f"unknown evaluator provider: {self.provider_id}")
        if self.provider_id == "process.harness":
            if self.command_ref is None:
                raise ValueError("process.harness evaluator requires a command reference")
            if self.browser_scenario is not None:
                raise ValueError("process.harness evaluator cannot declare a browser scenario")
        elif self.provider_id == BROWSER_EVALUATOR_ID:
            if self.browser_scenario is None:
                raise ValueError("browser.playwright evaluator requires a browser scenario")
            if self.command_ref is not None:
                raise ValueError("browser.playwright evaluator cannot name a process command")
        elif self.command_ref is not None or self.browser_scenario is not None:
            raise ValueError("unsupported evaluator execution configuration")
        return self


class HarnessVerification(HarnessModel):
    schema_name: ClassVar[str] = "harness_verification"
    required: tuple[str, ...] = ()
    required_evaluators: tuple[str, ...] = ()
    evidence_freshness: Literal["exact_diff", "exact_tree"] = "exact_diff"
    review: HarnessReview = HarnessReview()


class NetworkMode(StableStrEnum):
    DISABLED = "disabled"
    RESTRICTED = "restricted"
    FULL = "full"


class HarnessNetwork(HarnessModel):
    schema_name: ClassVar[str] = "harness_network"
    mode: NetworkMode = NetworkMode.DISABLED
    https_domains: tuple[str, ...] = ()
    ports: tuple[int, ...] = ()

    @model_validator(mode="after")
    def _disabled_has_no_authority(self) -> Self:
        if self.mode is NetworkMode.DISABLED and (self.https_domains or self.ports):
            raise ValueError("disabled network cannot declare domains or ports")
        if len(self.https_domains) != len(set(self.https_domains)):
            raise ValueError("network domains must be unique")
        invalid_port = any(not 1 <= port <= 65535 for port in self.ports)
        if len(self.ports) != len(set(self.ports)) or invalid_port:
            raise ValueError("network ports must be unique values from 1 through 65535")
        return self


class InstallDisposition(StableStrEnum):
    ALLOW = "allow"
    APPROVAL = "approval"
    DENY = "deny"


class HarnessInstall(HarnessModel):
    schema_name: ClassVar[str] = "harness_install"
    ecosystems: tuple[Literal["python_venv", "node_project"], ...] = ()
    existing_lock: InstallDisposition = InstallDisposition.DENY
    new_dependency: InstallDisposition = InstallDisposition.DENY
    manifest_lock_mutation: InstallDisposition = InstallDisposition.DENY
    lifecycle_scripts: InstallDisposition = InstallDisposition.DENY
    new_registry_domain: InstallDisposition = InstallDisposition.DENY
    host_global: Literal["deny"] = "deny"

    @field_validator("ecosystems")
    @classmethod
    def _unique_ecosystems(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("install ecosystems must be unique")
        return value


class HarnessApprovals(HarnessModel):
    schema_name: ClassVar[str] = "harness_approvals"
    process_shell: Literal["required", "deny"] = "required"
    new_dependency: Literal["required", "deny"] = "required"
    manifest_lock_mutation: Literal["required", "deny"] = "required"
    lifecycle_scripts: Literal["required", "deny"] = "deny"
    new_registry_domain: Literal["required", "deny"] = "required"
    promotion: Literal["required", "policy"] = "required"


class HarnessWorker(HarnessModel):
    schema_name: ClassVar[str] = "harness_worker"
    preferred: Literal["codex_cli", "claude_code_cli", "ollama_cli"] | None = None
    allowed: tuple[Literal["codex_cli", "claude_code_cli", "ollama_cli"], ...] = ()
    allowed_strategy_ids: tuple[str, ...] = ()
    adaptive_routing: bool = False
    local_backend: bool = False
    isolated_workspace_tools: bool = False

    @field_validator("allowed_strategy_ids")
    @classmethod
    def _unique_nonblank_strategy_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not strategy_id.strip() for strategy_id in value):
            raise ValueError("allowed strategy IDs must be non-blank")
        if len(value) != len(set(value)):
            raise ValueError("allowed strategy IDs must be unique")
        return value

    @model_validator(mode="after")
    def _preferred_is_allowed(self) -> Self:
        if len(self.allowed) != len(set(self.allowed)):
            raise ValueError("allowed workers must be unique")
        if self.preferred is not None and self.preferred not in self.allowed:
            raise ValueError("preferred worker must appear in allowed workers")
        return self


class HarnessBudgets(HarnessModel):
    schema_name: ClassVar[str] = "harness_budgets"
    wall_seconds: float = Field(default=1800.0, gt=0)
    worker_turns: int = Field(default=12, ge=0)
    processes: int = Field(default=40, ge=0)
    download_bytes: int = Field(default=0, ge=0)
    artifact_bytes: int = Field(default=100_000_000, ge=0)

    @field_validator("wall_seconds")
    @classmethod
    def _finite_wall_seconds(cls, value: float) -> float:
        import math

        if not math.isfinite(value):
            raise ValueError("wall_seconds must be finite")
        return value


class HarnessIncidentReporting(HarnessModel):
    """Repository-scoped incident-reporting intent without publishing authority."""

    schema_name: ClassVar[str] = "harness_incident_reporting"
    mode: Literal["off", "approval_required", "auto"] = "off"
    target_repository: str | None = Field(default=None, max_length=140)
    repository_key_env: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    auto_categories: tuple[
        Literal["trust_kernel_failure", "persistence_failure", "worker_boundary_failure"], ...
    ] = ()
    auto_failures: tuple[
        Literal["assertion_error", "os_error", "runtime_error", "type_error", "value_error"], ...
    ] = ()
    outbox_path: str = "~/.fleet/incident-reporting/outbox.sqlite3"
    retention_hours: int = Field(default=168, ge=1, le=720)
    approval_hours: int = Field(default=24, ge=1, le=168)
    daily_limit: int = Field(default=3, ge=1, le=20)
    pending_cap: int = Field(default=20, ge=1, le=100)

    @field_validator("target_repository")
    @classmethod
    def _public_github_repository(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}",
            value,
        ):
            raise ValueError("target repository must be a public GitHub owner/repository slug")
        return value

    @field_validator("outbox_path")
    @classmethod
    def _private_local_outbox(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or "\x00" in value
            or "://" in value
            or not (value.startswith("/") or value.startswith("~/"))
            or ".." in path.parts
        ):
            raise ValueError("incident outbox must be an absolute or home-relative local path")
        return value

    @model_validator(mode="after")
    def _authority_is_explicit_and_narrow(self) -> Self:
        active = self.target_repository is not None or self.repository_key_env is not None
        if self.mode == "off":
            if active or self.auto_categories or self.auto_failures:
                raise ValueError("off incident reporting cannot declare active authority")
            return self
        if not self.target_repository or not self.repository_key_env:
            raise ValueError(
                "enabled incident reporting requires a target repository "
                "and key environment variable"
            )
        if self.mode != "auto" and (self.auto_categories or self.auto_failures):
            raise ValueError("auto allowlists require auto incident reporting")
        if self.mode == "auto" and (not self.auto_categories or not self.auto_failures):
            raise ValueError("auto incident reporting requires category and failure allowlists")
        if len(self.auto_categories) != len(set(self.auto_categories)):
            raise ValueError("incident category allowlist must be unique")
        if len(self.auto_failures) != len(set(self.auto_failures)):
            raise ValueError("incident failure allowlist must be unique")
        return self


class ProjectHarnessV2(HarnessModel):
    """Repository intent; provisional instances grant no execution authority."""

    schema_name: ClassVar[str] = "project_harness"
    schema_version: Literal[2] = 2
    commands: Mapping[str, HarnessCommand] = Field(default_factory=lambda: FrozenDict({}))
    evaluators: tuple[HarnessEvaluator, ...] = ()
    rules: tuple[ProvenancedValue, ...] = ()
    paths: HarnessPaths = HarnessPaths()
    verification: HarnessVerification = HarnessVerification()
    network: HarnessNetwork = HarnessNetwork()
    install: HarnessInstall = HarnessInstall()
    approvals: HarnessApprovals = HarnessApprovals()
    worker: HarnessWorker = HarnessWorker()
    budgets: HarnessBudgets = HarnessBudgets()
    incident_reporting: HarnessIncidentReporting = HarnessIncidentReporting()
    provisional: bool = False
    migrated_from: Literal["1"] | None = None

    @field_validator("commands")
    @classmethod
    def _freeze_commands(cls, value: Mapping[str, HarnessCommand]) -> Mapping[str, HarnessCommand]:
        if any(not name for name in value):
            raise ValueError("command names must be non-empty")
        return FrozenDict(value)

    @field_serializer("commands")
    def _serialize_commands(self, value: Mapping[str, HarnessCommand]) -> dict[str, HarnessCommand]:
        return dict(value)

    @model_validator(mode="after")
    def _validate_references_and_provisional_authority(self) -> Self:
        missing = set(self.verification.required) - set(self.commands)
        if missing:
            raise ValueError(f"verification references unknown commands: {sorted(missing)}")
        evaluator_ids = tuple(item.id for item in self.evaluators)
        if len(evaluator_ids) != len(set(evaluator_ids)):
            raise ValueError("evaluator IDs must be unique")
        criterion_ids = tuple(
            criterion_id
            for evaluator in self.evaluators
            for criterion_id in evaluator.criterion_ids
        )
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("evaluator criterion IDs must be globally unique")
        missing_evaluators = set(self.verification.required_evaluators) - set(evaluator_ids)
        if missing_evaluators:
            raise ValueError(
                f"verification references unknown evaluators: {sorted(missing_evaluators)}"
            )
        missing_commands = {
            item.command_ref
            for item in self.evaluators
            if item.command_ref is not None and item.command_ref not in self.commands
        }
        if missing_commands:
            raise ValueError(f"evaluators reference unknown commands: {sorted(missing_commands)}")
        if self.provisional:
            if self.approvals.promotion == "policy":
                raise ValueError("provisional Harness cannot grant promotion auto-approval")
            if self.incident_reporting.mode != "off":
                raise ValueError("provisional Harness cannot enable incident reporting")
            if self.network.mode is not NetworkMode.DISABLED:
                raise ValueError("provisional Harness cannot grant network authority")
            if self.install.ecosystems or self.install.existing_lock is not InstallDisposition.DENY:
                raise ValueError("provisional Harness cannot grant install authority")
            if (
                self.evaluators
                or self.verification.required_evaluators
                or self.worker.allowed
                or self.worker.allowed_strategy_ids
                or self.worker.adaptive_routing
                or self.worker.local_backend
            ):
                raise ValueError("provisional Harness cannot grant worker authority")
        return self


def project_profile_v1_to_harness(profile: ProjectProfile) -> ProjectHarnessV2:
    """Convert v1 content to a safe, provisional in-memory v2 candidate."""

    import shlex

    commands: dict[str, HarnessCommand] = {}
    if isinstance(profile.commands, Mapping):
        for name, raw in profile.commands.items():
            if isinstance(name, str) and isinstance(raw, str) and raw.strip():
                commands[name] = HarnessCommand(
                    argv=tuple(shlex.split(raw)), cwd=PurePosixPath(profile.root).as_posix()
                )
    return ProjectHarnessV2(
        commands=commands,
        rules=profile.rules,
        paths=HarnessPaths(
            protected=profile.protected_paths or (".git/**",),
            generated=profile.generated_paths,
        ),
        network=HarnessNetwork(),
        install=HarnessInstall(),
        approvals=HarnessApprovals(),
        worker=HarnessWorker(),
        provisional=True,
        migrated_from="1",
    )
