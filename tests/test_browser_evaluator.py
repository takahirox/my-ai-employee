from __future__ import annotations

import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_employee.domain.browser import (
    BrowserAction,
    BrowserCapture,
    BrowserObservation,
    BrowserScenario,
)
from ai_employee.domain.evaluation import (
    EvaluationBudget,
    EvaluationRequest,
    EvaluatorBehavior,
    EvaluatorSpecification,
)
from ai_employee.domain.v2 import ArtifactDescriptor, StableFailure, StableFailureCode
from ai_employee.evaluators import DEFAULT_EVALUATOR_REGISTRY, BrowserPlaywrightEvaluator
from ai_employee.serialization import versioned_digest

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = "0" * 64
ONE = "1" * 64


def scenario(target: str = "http://127.0.0.1:3000/app") -> BrowserScenario:
    return BrowserScenario(
        origin="http://127.0.0.1:3000",
        actions=(
            BrowserAction(kind="navigate", url=target),
            BrowserAction(kind="click", selector="#submit"),
        ),
        captures=(
            BrowserCapture(id="screen", kind="screenshot", logical_kind="browser_screenshot"),
        ),
        timeout_seconds=5.0,
    )


def specification() -> EvaluatorSpecification:
    provider = BrowserPlaywrightEvaluator()
    return EvaluatorSpecification(
        id="spec-1",
        run_id="run-1",
        created_at=NOW,
        provider_id=provider.descriptor.provider_id,
        provider_schema_version=provider.descriptor.provider_schema_version,
        provider_descriptor_digest=provider.descriptor_digest,
        behavior=EvaluatorBehavior.DETERMINISTIC,
        required_capabilities=("browser",),
        requested_observation_kinds=("browser_screenshot",),
        browser_scenario=scenario(),
        criterion_ids=("browser-safe",),
    )


def request(spec: EvaluatorSpecification, **changes: object) -> EvaluationRequest:
    budget: dict[str, object] = {
        "remaining_processes": 0,
        "remaining_artifact_bytes": 1_024,
        "remaining_actions": 2,
        "remaining_duration_seconds": 5.0,
    }
    budget.update(changes)
    return EvaluationRequest(
        id="request-1",
        run_id="run-1",
        created_at=NOW,
        candidate_digest=ZERO,
        generation=0,
        evaluator_specification_digest=spec.content_digest or "",
        effective_policy_digest=ZERO,
        remaining_budget=EvaluationBudget.model_validate(budget, strict=True),
    )


class FakeBrowserServices:
    def __init__(
        self,
        status: str = "succeeded",
        *,
        duration: float = 1.0,
        artifact_size: int = 10,
        final_url: str = "http://127.0.0.1:3000/done",
        actions: int | None = None,
        error: Exception | None = None,
    ) -> None:
        self.status, self.duration, self.artifact_size = status, duration, artifact_size
        self.final_url, self.actions, self.error = final_url, actions, error
        self.cancelled_value = False
        self.opens = 0
        self.teardowns: list[str] = []
        self.received: BrowserScenario | None = None
        self.count = 0

    def new_id(self, prefix: str) -> str:
        self.count += 1
        return f"{prefix}-{self.count}"

    def created_at(self) -> datetime:
        return NOW

    def cancelled(self) -> bool:
        return self.cancelled_value

    def open_browser(self, value: BrowserScenario, _request: EvaluationRequest) -> str:
        self.opens += 1
        self.received = value
        return "session-1"

    def observe_browser(
        self, session: str, value: BrowserScenario, evaluation_request: EvaluationRequest
    ) -> BrowserObservation:
        if self.error:
            raise self.error
        failure = None
        if self.status != "succeeded":
            code = {
                "failed": StableFailureCode.INVALID_REQUEST,
                "cancelled": StableFailureCode.CANCELLED,
                "timed_out": StableFailureCode.TIMEOUT,
            }[self.status]
            failure = StableFailure(code=code, message=f"browser {self.status}")
        observation_id = "observation-1"
        artifacts: tuple[ArtifactDescriptor, ...] = ()
        if self.status == "succeeded":
            artifacts = (
                ArtifactDescriptor(
                    id="artifact-1",
                    run_id=evaluation_request.run_id,
                    created_at=NOW,
                    artifact_digest=ONE,
                    media_type="image/png",
                    size_bytes=self.artifact_size,
                    logical_kind="browser_screenshot",
                    producer_action_id=observation_id,
                    source={
                        "request_digest": evaluation_request.content_digest,
                        "scenario_digest": versioned_digest(value),
                        "session_id": session,
                    },
                    store_locator=f"sha256/{ONE[:2]}/{ONE}",
                ),
            )
        completed = self.actions
        if completed is None:
            completed = len(value.actions) if self.status == "succeeded" else 1
        return BrowserObservation(
            id=observation_id,
            run_id=evaluation_request.run_id,
            created_at=NOW,
            request_digest=evaluation_request.content_digest or "",
            scenario_digest=versioned_digest(value),
            session_id=session,
            status=self.status,
            final_url=self.final_url,  # type: ignore[arg-type]
            actions_completed=completed,
            duration_seconds=self.duration,
            artifacts=artifacts,
            failure=failure,
        )

    def teardown_browser(self, session: str) -> None:
        self.teardowns.append(session)


def test_static_mediated_provider_has_fixed_security_intent_and_no_dependency() -> None:
    provider = DEFAULT_EVALUATOR_REGISTRY.resolve("browser.playwright")
    assert isinstance(provider, BrowserPlaywrightEvaluator)
    dependencies = tomllib.loads(Path("pyproject.toml").read_text())["project"]["dependencies"]
    assert all("playwright" not in item.lower() for item in dependencies)
    spec, services = specification(), FakeBrowserServices()
    provider.evaluate(request(spec), spec, services)  # type: ignore[arg-type]
    assert services.received is not None
    assert services.received.profile_mode == "isolated_ephemeral"
    assert services.received.inherit_credentials is False
    assert services.received.external_request_policy == "deny"


@pytest.mark.parametrize(
    "target", ["file:///tmp/x", "https://example.com", "http://127.0.0.1:3001"]
)
def test_scenario_rejects_non_loopback_and_cross_origin(target: str) -> None:
    with pytest.raises(ValidationError):
        scenario(target)
    with pytest.raises(ValidationError):
        BrowserScenario.model_validate(
            {
                **scenario().model_dump(),
                "inherit_credentials": True,
            },
            strict=True,
        )


@pytest.mark.parametrize(
    ("budget", "message"),
    [
        ({"remaining_actions": 1}, "action budget"),
        ({"remaining_duration_seconds": 4.0}, "duration budget"),
        ({"remaining_artifact_bytes": 0}, "artifact budget"),
    ],
)
def test_preflight_budgets_prevent_launch(budget: dict[str, object], message: str) -> None:
    spec, services = specification(), FakeBrowserServices()
    with pytest.raises(ValueError, match=message):
        BrowserPlaywrightEvaluator().evaluate(request(spec, **budget), spec, services)  # type: ignore[arg-type]
    assert services.opens == 0


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "timed_out"])
def test_teardown_covers_every_terminal_status(status: str) -> None:
    spec, services = specification(), FakeBrowserServices(status)
    BrowserPlaywrightEvaluator().evaluate(request(spec), spec, services)  # type: ignore[arg-type]
    assert services.teardowns == ["session-1"]


@pytest.mark.parametrize(
    ("services", "message"),
    [
        (FakeBrowserServices(actions=3), "action count"),
        (FakeBrowserServices(duration=6.0), "timeout"),
        (FakeBrowserServices(artifact_size=1_025), "artifact byte budget"),
        (FakeBrowserServices(final_url="http://127.0.0.1:3001"), "escaped"),
        (FakeBrowserServices(error=RuntimeError("crashed")), "crashed"),
    ],
)
def test_reported_limits_and_failures_teardown(services: FakeBrowserServices, message: str) -> None:
    spec = specification()
    with pytest.raises((RuntimeError, ValueError), match=message):
        BrowserPlaywrightEvaluator().evaluate(request(spec), spec, services)  # type: ignore[arg-type]
    assert services.teardowns == ["session-1"]


def test_cancellation_and_malformed_specification_fail_closed() -> None:
    spec, services = specification(), FakeBrowserServices()
    services.cancelled_value = True
    with pytest.raises(ValueError, match="cancelled before launch"):
        BrowserPlaywrightEvaluator().evaluate(request(spec), spec, services)  # type: ignore[arg-type]
    assert services.opens == 0
    payload = spec.model_dump(exclude={"content_digest", "digest_metadata"})
    payload["browser_scenario"] = None
    with pytest.raises(ValidationError, match="typed scenario"):
        EvaluatorSpecification.model_validate(payload, strict=True)
