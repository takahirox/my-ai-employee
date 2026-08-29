from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_employee.domain.browser import BrowserAction, BrowserCapture, BrowserScenario
from ai_employee.domain.evaluation import EvaluationBudget, EvaluationRequest
from ai_employee.services_v2 import AtomicArtifactStore
from ai_employee.services_v2.browser import (
    PLAYWRIGHT_UNAVAILABLE_MESSAGE,
    BrowserRequestRejected,
    PlaywrightBrowserEvaluationServices,
    PlaywrightRouteRequest,
    RouteHandler,
    default_playwright_engine_factory,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = "0" * 64


class Cancellation:
    value = False

    def cancelled(self) -> bool:
        return self.value


class FakeEngine:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.route: RouteHandler | None = None
        self.actions: list[tuple[object, ...]] = []
        self.closed: list[str] = []
        self.url = "http://127.0.0.1:3000/index.html"

    def open(self, route_handler: RouteHandler) -> None:
        self.route = route_handler
        if self.error is not None:
            raise self.error

    def navigate(self, url: str, timeout_seconds: float) -> None:
        self.actions.append(("navigate", url, timeout_seconds))

    def click(self, selector: str, timeout_seconds: float) -> None:
        self.actions.append(("click", selector, timeout_seconds))

    def fill(self, selector: str, value: str, timeout_seconds: float) -> None:
        self.actions.append(("fill", selector, value, timeout_seconds))

    def screenshot(self, timeout_seconds: float) -> bytes:
        return b"png"

    def console(self) -> tuple[dict[str, object], ...]:
        return ({"type": "log", "text": "ready"},)

    def dom(self, timeout_seconds: float) -> str:
        return "<html><body>ready</body></html>"

    def accessibility(self, timeout_seconds: float) -> object:
        return {"role": "document", "name": "ready"}

    def current_url(self) -> str:
        return self.url

    def close_page(self) -> None:
        self.closed.append("page")

    def close_context(self) -> None:
        self.closed.append("context")

    def close_browser(self) -> None:
        self.closed.append("browser")

    def close_engine(self) -> None:
        self.closed.append("engine")


def scenario() -> BrowserScenario:
    return BrowserScenario(
        origin="http://127.0.0.1:3000",
        actions=(
            BrowserAction(kind="navigate", url="http://127.0.0.1:3000/index.html"),
            BrowserAction(kind="click", selector="#submit"),
            BrowserAction(kind="fill", selector="#name", value="Ada"),
        ),
        captures=(
            BrowserCapture(id="screen", kind="screenshot", logical_kind="browser_screenshot"),
            BrowserCapture(id="console", kind="console", logical_kind="browser_console"),
            BrowserCapture(id="dom", kind="dom", logical_kind="browser_dom"),
            BrowserCapture(
                id="accessibility",
                kind="accessibility",
                logical_kind="browser_accessibility",
            ),
        ),
        timeout_seconds=5.0,
    )


def request(value: BrowserScenario) -> EvaluationRequest:
    return EvaluationRequest(
        id="request-1",
        run_id="run-1",
        created_at=NOW,
        candidate_digest=ZERO,
        generation=0,
        evaluator_specification_digest=ZERO,
        effective_policy_digest=ZERO,
        remaining_budget=EvaluationBudget(
            remaining_processes=0,
            remaining_artifact_bytes=100_000,
            remaining_actions=len(value.actions),
            remaining_duration_seconds=value.timeout_seconds,
        ),
    )


def services(
    root: Path,
    engine: FakeEngine,
    cancellation: Cancellation | None = None,
    *,
    maximum_artifact_bytes: int = 100_000,
) -> tuple[PlaywrightBrowserEvaluationServices, AtomicArtifactStore]:
    artifacts = AtomicArtifactStore(root / "artifacts")
    counter = iter(range(100))
    result = PlaywrightBrowserEvaluationServices(
        root / "workspace",
        artifacts,
        cancellation or Cancellation(),
        engine_factory=lambda: engine,
        id_factory=lambda prefix: f"{prefix}-{next(counter)}",
        clock=lambda: NOW,
        monotonic=lambda: 1.0,
        maximum_artifact_bytes=maximum_artifact_bytes,
    )
    return result, artifacts


def test_fake_engine_executes_only_typed_actions_and_persists_bound_artifacts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text("<button id='submit'>go</button>")
    engine = FakeEngine()
    service, artifacts = services(tmp_path, engine)
    value = scenario()
    evaluation_request = request(value)

    session_id = service.open_browser(value, evaluation_request)
    assert engine.route is not None
    response = engine.route(
        PlaywrightRouteRequest(
            method="GET",
            url="http://127.0.0.1:3000/index.html",
            resource_type="document",
        )
    )
    assert response.body == b"<button id='submit'>go</button>"
    observation = service.observe_browser(session_id, value, evaluation_request)

    assert observation.status == "succeeded"
    assert [item[0] for item in engine.actions] == ["navigate", "click", "fill"]
    assert tuple(item.logical_kind for item in observation.artifacts) == (
        "browser_screenshot",
        "browser_console",
        "browser_dom",
        "browser_accessibility",
    )
    for descriptor in observation.artifacts:
        assert descriptor.producer_action_id == observation.id
        assert descriptor.source["request_digest"] == evaluation_request.content_digest
        assert descriptor.source["scenario_digest"] == observation.scenario_digest
        assert descriptor.source["session_id"] == session_id
        with artifacts.open_verified(descriptor) as stream:
            assert stream.read()
    service.teardown_browser(session_id)
    assert engine.closed == ["page", "context", "browser", "engine"]


@pytest.mark.parametrize(
    "route_request",
    [
        PlaywrightRouteRequest(
            method="GET",
            url="https://example.com/index.html",
            resource_type="document",
        ),
        PlaywrightRouteRequest(
            method="GET",
            url="http://127.0.0.1:3000/index.html",
            resource_type="document",
            is_redirect=True,
        ),
        PlaywrightRouteRequest(
            method="GET",
            url="http://127.0.0.1:3000/index.html",
            resource_type="websocket",
        ),
        PlaywrightRouteRequest(
            method="GET",
            url="http://127.0.0.1:3000/index.html",
            resource_type="fetch",
            is_background=True,
        ),
        PlaywrightRouteRequest(
            method="POST",
            url="http://127.0.0.1:3000/index.html",
            resource_type="document",
        ),
        PlaywrightRouteRequest(
            method="GET",
            url="http://127.0.0.1:3000/%2e%2e/secret",
            resource_type="document",
        ),
    ],
)
def test_route_rejects_every_non_workspace_request(
    tmp_path: Path,
    route_request: PlaywrightRouteRequest,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text("ok")
    engine = FakeEngine()
    service, _artifacts = services(tmp_path, engine)
    session_id = service.open_browser(scenario(), request(scenario()))
    assert engine.route is not None
    with pytest.raises(BrowserRequestRejected):
        engine.route(route_request)
    service.teardown_browser(session_id)


def test_route_rejects_symlink_that_escapes_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.html"
    outside.write_text("secret")
    (workspace / "escape.html").symlink_to(outside)
    engine = FakeEngine()
    service, _artifacts = services(tmp_path, engine)
    value = scenario()
    session_id = service.open_browser(value, request(value))
    assert engine.route is not None
    with pytest.raises(BrowserRequestRejected, match="escapes"):
        engine.route(
            PlaywrightRouteRequest(
                method="GET",
                url="http://127.0.0.1:3000/escape.html",
                resource_type="document",
            )
        )
    service.teardown_browser(session_id)


def test_open_failure_and_terminal_observations_close_every_layer(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    value = scenario()
    failing = FakeEngine(RuntimeError("launch failed"))
    service, _artifacts = services(tmp_path, failing)
    with pytest.raises(RuntimeError, match="launch failed"):
        service.open_browser(value, request(value))
    assert failing.closed == ["page", "context", "browser", "engine"]

    for error, expected in (
        (TimeoutError(), "timed_out"),
        (BrowserRequestRejected("external"), "failed"),
    ):
        engine = FakeEngine()

        def fail_action(
            _url: str,
            _timeout: float,
            failure: Exception = error,
        ) -> None:
            raise failure

        engine.navigate = fail_action  # type: ignore[method-assign]
        service, _artifacts = services(tmp_path, engine)
        session_id = service.open_browser(value, request(value))
        assert service.observe_browser(session_id, value, request(value)).status == expected
        service.teardown_browser(session_id)
        assert engine.closed == ["page", "context", "browser", "engine"]


def test_cancellation_and_artifact_bound_are_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    value = scenario()

    cancellation = Cancellation()
    engine = FakeEngine()
    service, _artifacts = services(tmp_path, engine, cancellation)
    session_id = service.open_browser(value, request(value))
    cancellation.value = True
    observation = service.observe_browser(session_id, value, request(value))
    assert observation.status == "cancelled"
    service.teardown_browser(session_id)

    engine = FakeEngine()
    service, _artifacts = services(tmp_path, engine, maximum_artifact_bytes=2)
    session_id = service.open_browser(value, request(value))
    observation = service.observe_browser(session_id, value, request(value))
    assert observation.status == "failed"
    assert observation.artifacts == ()
    service.teardown_browser(session_id)


def test_default_factory_has_stable_missing_dependency_error() -> None:
    with (
        patch.dict("sys.modules", {"playwright": None}),
        pytest.raises(RuntimeError) as captured,
    ):
        default_playwright_engine_factory()
    assert str(captured.value) == PLAYWRIGHT_UNAVAILABLE_MESSAGE
