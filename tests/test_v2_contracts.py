from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ai_employee.domain import (
    ActionProposal,
    PolicyLayer,
    PolicyLayerKind,
    PolicyResolutionError,
    PolicyResolver,
    ProcessRequest,
    StableFailureCode,
)
from ai_employee.domain.policy_v2 import NetworkMode
from ai_employee.domain.v2 import ActionKind
from ai_employee.serialization import canonical_json, versioned_digest

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def process_request(*, created_at: datetime = NOW) -> ProcessRequest:
    return ProcessRequest(
        id="request.one",
        run_id="run.one",
        created_at=created_at,
        argv=("python", "-m", "pytest"),
        purpose="verify",
    )


def proposal() -> ActionProposal:
    return ActionProposal(
        id="proposal.one",
        run_id="run.one",
        created_at=NOW,
        worker_id="worker.one",
        kind=ActionKind.PROCESS,
        payload=process_request(),
        reason="run verification",
    )


def layer(kind: PolicyLayerKind, **updates: object) -> PolicyLayer:
    values: dict[str, object] = {
        "id": f"layer.{kind.value}",
        "run_id": "run.one",
        "created_at": NOW,
        "kind": kind,
    }
    if kind is PolicyLayerKind.BUILTIN:
        values.update(
            {
                "allowed_capabilities": ("process", "download"),
                "writable_paths": ("src/**", "tests/**"),
                "https_domains": ("example.com", "files.example.com"),
                "network_mode": NetworkMode.RESTRICTED,
                "process_shell_allowed": False,
                "install_ecosystems": ("python_venv", "node_project"),
                "max_wall_seconds": 600.0,
                "max_processes": 20,
                "max_worker_turns": 10,
                "max_download_bytes": 1000,
                "max_artifact_bytes": 2000,
                "required_approvals": ("promotion",),
            }
        )
    values.update(updates)
    return PolicyLayer(**values)  # type: ignore[arg-type]


def test_v2_request_is_strict_frozen_and_digest_excludes_identity_time() -> None:
    first = process_request()
    second = process_request(created_at=NOW + timedelta(days=1))
    assert first.content_digest == second.content_digest
    rebound_identity = first.model_copy(update={"id": "request.other", "run_id": "run.other"})
    rebound_identity = ProcessRequest.model_validate_json(
        canonical_json(rebound_identity), strict=True
    )
    assert first.content_digest == rebound_identity.content_digest
    with pytest.raises(ValidationError):
        ProcessRequest(
            id="request.two",
            run_id="run.one",
            created_at=NOW,
            argv=("true",),
            purpose="x",
            unexpected=True,  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        first.argv = ("false",)  # type: ignore[misc]
    with pytest.raises(ValidationError):
        process_request().model_copy(update={"content_digest": "f" * 64}).model_validate(
            process_request().model_copy(update={"content_digest": "f" * 64}), strict=True
        )
    first_proposal = proposal()
    later_proposal = first_proposal.model_copy(
        update={"payload": process_request(created_at=NOW + timedelta(days=1))}
    )
    rebound = ActionProposal.model_validate_json(canonical_json(later_proposal), strict=True)
    assert rebound.content_digest == first_proposal.content_digest


def test_versioned_digest_fails_closed_and_retains_explicit_null() -> None:
    assert versioned_digest({"value": None}) != versioned_digest({})
    assert versioned_digest({"b": "日本語", "a": 1}) == versioned_digest({"a": 1, "b": "日本語"})
    with pytest.raises(ValueError, match="unsupported digest algorithm"):
        versioned_digest({}, algorithm="sha512")
    with pytest.raises(ValueError, match="unsupported digest format"):
        versioned_digest({}, format_version="2")


@pytest.mark.parametrize(
    ("environment", "cwd"),
    [
        ((("GITHUB_AUTH", "raw-secret"),), "."),
        ((("AWS_ACCESS_KEY_ID", "raw-secret"),), "."),
        ((), "src//package"),
        ((), "src/"),
    ],
)
def test_process_request_rejects_raw_credentials_and_noncanonical_paths(
    environment: tuple[tuple[str, str], ...], cwd: str
) -> None:
    with pytest.raises(ValidationError):
        ProcessRequest(
            id="request.unsafe",
            run_id="run.one",
            created_at=NOW,
            argv=("true",),
            cwd=cwd,
            environment=environment,
            purpose="reject unsafe request",
        )


def test_stable_failure_code_registry_contains_required_codes() -> None:
    required = {
        "POLICY_DENIED",
        "APPROVAL_REQUIRED",
        "APPROVAL_EXPIRED",
        "INVALID_REQUEST",
        "BUDGET_EXCEEDED",
        "TIMEOUT",
        "CANCELLED",
        "SPAWN_FAILED",
        "PROCESS_FAILED",
        "NETWORK_BLOCKED",
        "DNS_REBIND_BLOCKED",
        "TLS_FAILED",
        "INTEGRITY_FAILED",
        "INSTALL_DENIED",
        "WORKER_UNAVAILABLE",
        "WORKER_PROTOCOL_ERROR",
        "EVALUATOR_EXECUTION_UNAVAILABLE",
        "VERIFICATION_FAILED",
        "REVIEW_BLOCKED",
        "WORKSPACE_CONFLICT",
        "PROMOTION_FAILED",
    }
    assert required <= {item.value for item in StableFailureCode}


def test_policy_resolver_intersects_unions_and_minimizes() -> None:
    result = PolicyResolver().resolve(
        proposal(),
        (
            layer(PolicyLayerKind.BUILTIN),
            layer(
                PolicyLayerKind.OPERATOR,
                allowed_capabilities=("process",),
                writable_paths=("src/**",),
                https_domains=("example.com",),
                install_ecosystems=("python_venv",),
                max_processes=5,
                required_approvals=("process",),
            ),
            layer(PolicyLayerKind.PROJECT, denied_capabilities=("download",)),
        ),
        decision_id="decision.one",
        created_at=NOW,
    )
    effective = result.effective_policy
    assert effective.allowed_capabilities == ("process",)
    assert effective.writable_paths == ("src/**",)
    assert effective.max_processes == 5
    assert effective.denied_capabilities == ("download",)
    assert effective.required_approvals == ("process", "promotion")
    assert result.decision.outcome.value == "approval_required"
    assert result.decision.required_approval_classes == ("process",)
    assert result.decision.effective_policy_digest == effective.content_digest


def test_unrelated_approval_class_does_not_gate_process() -> None:
    result = PolicyResolver().resolve(
        proposal(),
        (layer(PolicyLayerKind.BUILTIN),),
        decision_id="decision.one",
        created_at=NOW,
    )
    assert result.effective_policy.required_approvals == ("promotion",)
    assert result.decision.outcome.value == "allow"
    assert result.decision.required_approval_classes == ()


@pytest.mark.parametrize(
    "updates",
    [
        {"allowed_capabilities": ("process", "review")},
        {"network_mode": NetworkMode.FULL},
        {"process_shell_allowed": True},
        {"max_processes": 21},
    ],
)
def test_lower_policy_layers_cannot_increase_authority(updates: dict[str, object]) -> None:
    with pytest.raises(PolicyResolutionError) as raised:
        PolicyResolver().resolve(
            proposal(),
            (layer(PolicyLayerKind.BUILTIN), layer(PolicyLayerKind.PROJECT, **updates)),
            decision_id="decision.one",
            created_at=NOW,
        )
    assert raised.value.code == "POLICY_AUTHORITY_INCREASE"


def test_policy_layer_order_and_builtin_completeness_fail_closed() -> None:
    resolver = PolicyResolver()
    with pytest.raises(PolicyResolutionError, match="built-in"):
        resolver.resolve(
            proposal(),
            (layer(PolicyLayerKind.PROJECT),),
            decision_id="decision.one",
            created_at=NOW,
        )
    with pytest.raises(PolicyResolutionError) as raised:
        resolver.resolve(
            proposal(),
            (
                PolicyLayer(
                    id="builtin",
                    run_id="run.one",
                    created_at=NOW,
                    kind=PolicyLayerKind.BUILTIN,
                ),
            ),
            decision_id="decision.one",
            created_at=NOW,
        )
    assert raised.value.code == "POLICY_INCOMPLETE_BUILTIN"
