"""Confined local-file Playwright services for browser evaluation."""

from __future__ import annotations

import json
import mimetypes
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from urllib.parse import unquote, urlsplit

from ai_employee.domain.base import FrozenDict
from ai_employee.domain.browser import (
    BrowserAction,
    BrowserObservation,
    BrowserScenario,
    browser_origin,
)
from ai_employee.domain.evaluation import EvaluationRequest
from ai_employee.domain.services_v2 import ArtifactStore, Cancellation
from ai_employee.domain.v2 import (
    ArtifactDescriptor,
    ArtifactPutRequest,
    StableFailure,
    StableFailureCode,
)
from ai_employee.serialization import versioned_digest

from ._common import identifier, now

PLAYWRIGHT_UNAVAILABLE_MESSAGE = (
    "browser evaluation unavailable: install the 'browser' extra and its Chromium browser"
)


class PlaywrightUnavailableError(RuntimeError):
    """Stable failure raised when the optional runtime is not installed."""


class BrowserRequestRejected(RuntimeError):
    """A browser request was denied by the workspace-only route."""


@dataclass(frozen=True)
class PlaywrightRouteRequest:
    method: str
    url: str
    resource_type: str
    is_redirect: bool = False
    is_background: bool = False


@dataclass(frozen=True)
class PlaywrightRouteResponse:
    status: int
    media_type: str
    body: bytes


RouteHandler = Callable[[PlaywrightRouteRequest], PlaywrightRouteResponse]


class PlaywrightEngine(Protocol):
    """Small synchronous engine boundary; tests do not import or launch Playwright."""

    def open(self, route_handler: RouteHandler) -> None: ...

    def navigate(self, url: str, timeout_seconds: float) -> None: ...

    def click(self, selector: str, timeout_seconds: float) -> None: ...

    def fill(self, selector: str, value: str, timeout_seconds: float) -> None: ...

    def screenshot(self, timeout_seconds: float) -> bytes: ...

    def console(self) -> tuple[Mapping[str, object], ...]: ...

    def dom(self, timeout_seconds: float) -> str: ...

    def accessibility(self, timeout_seconds: float) -> object: ...

    def current_url(self) -> str | None: ...

    def close_page(self) -> None: ...

    def close_context(self) -> None: ...

    def close_browser(self) -> None: ...

    def close_engine(self) -> None: ...


EngineFactory = Callable[[], PlaywrightEngine]


class _SyncPlaywrightEngine:
    def __init__(self, sync_playwright: Callable[[], Any]) -> None:
        self._sync_playwright = sync_playwright
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._blocked: str | None = None
        self._console: list[Mapping[str, object]] = []

    def open(self, route_handler: RouteHandler) -> None:
        try:
            self._playwright = self._sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=(
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-domain-reliability",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                    "--no-proxy-server",
                ),
            )
            self._context = self._browser.new_context(
                accept_downloads=False,
                service_workers="block",
                storage_state=None,
                http_credentials=None,
                proxy=None,
            )
            self._context.route("**/*", lambda route: self._route(route, route_handler))
            self._page = self._context.new_page()
            self._page.on("console", self._record_console)
            self._page.on("download", lambda download: self._reject_event("download", download))
            self._page.on("popup", lambda popup: self._reject_event("background page", popup))
            self._page.on("worker", lambda worker: self._reject_event("worker", worker))
            self._page.on("websocket", lambda _socket: self._reject_event("websocket"))
            self._context.on(
                "page",
                lambda page: (
                    None if page is self._page else self._reject_event("background page", page)
                ),
            )
        except Exception:
            self._close_all()
            raise PlaywrightUnavailableError(PLAYWRIGHT_UNAVAILABLE_MESSAGE) from None

    def _route(self, route: Any, handler: RouteHandler) -> None:
        request = route.request
        resource_type = str(request.resource_type)
        try:
            response = handler(
                PlaywrightRouteRequest(
                    method=str(request.method),
                    url=str(request.url),
                    resource_type=resource_type,
                    is_redirect=request.redirected_from is not None,
                    is_background=resource_type
                    in {"eventsource", "manifest", "other", "websocket"},
                )
            )
        except BrowserRequestRejected as error:
            self._blocked = str(error)
            route.abort("blockedbyclient")
            return
        route.fulfill(
            status=response.status,
            headers={
                "cache-control": "no-store",
                "content-type": response.media_type,
                "content-security-policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
                    "base-uri 'none'; worker-src 'none'; form-action 'self'"
                ),
            },
            body=response.body if request.method != "HEAD" else b"",
        )

    def _record_console(self, message: Any) -> None:
        self._console.append(
            {
                "type": str(message.type),
                "text": str(message.text),
                "location": cast(object, message.location),
            }
        )

    def _reject_event(self, label: str, value: Any = None) -> None:
        self._blocked = f"{label} is forbidden"
        if value is None:
            return
        for method in ("cancel", "close"):
            callback = getattr(value, method, None)
            if callable(callback):
                with suppress(Exception):
                    callback()
                return

    def _perform(self, callback: Callable[[], object]) -> None:
        self._blocked = None
        try:
            callback()
            self._page.wait_for_timeout(0)
        except Exception as error:
            if self._blocked is not None:
                raise BrowserRequestRejected(self._blocked) from None
            if type(error).__name__ == "TimeoutError":
                raise TimeoutError("browser action timed out") from None
            raise
        if self._blocked is not None:
            raise BrowserRequestRejected(self._blocked)

    def navigate(self, url: str, timeout_seconds: float) -> None:
        self._perform(
            lambda: self._page.goto(
                url,
                wait_until="load",
                timeout=timeout_seconds * 1_000,
            )
        )

    def click(self, selector: str, timeout_seconds: float) -> None:
        self._perform(lambda: self._page.locator(selector).click(timeout=timeout_seconds * 1_000))

    def fill(self, selector: str, value: str, timeout_seconds: float) -> None:
        self._perform(
            lambda: self._page.locator(selector).fill(
                value,
                timeout=timeout_seconds * 1_000,
            )
        )

    def screenshot(self, timeout_seconds: float) -> bytes:
        return cast(bytes, self._page.screenshot(timeout=timeout_seconds * 1_000))

    def console(self) -> tuple[Mapping[str, object], ...]:
        return tuple(self._console)

    def dom(self, timeout_seconds: float) -> str:
        self._page.locator("html").wait_for(
            state="attached",
            timeout=timeout_seconds * 1_000,
        )
        return cast(str, self._page.content())

    def accessibility(self, timeout_seconds: float) -> object:
        return cast(
            object,
            self._page.locator("html").aria_snapshot(timeout=timeout_seconds * 1_000),
        )

    def current_url(self) -> str | None:
        return None if self._page is None else cast(str, self._page.url)

    def close_page(self) -> None:
        if self._page is not None:
            self._page.close()
            self._page = None

    def close_context(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None

    def close_browser(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    def close_engine(self) -> None:
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def _close_all(self) -> None:
        for callback in (
            self.close_page,
            self.close_context,
            self.close_browser,
            self.close_engine,
        ):
            with suppress(Exception):
                callback()


def default_playwright_engine_factory() -> PlaywrightEngine:
    """Load the optional package only when browser evaluation is requested."""

    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError:
        raise PlaywrightUnavailableError(PLAYWRIGHT_UNAVAILABLE_MESSAGE) from None
    return _SyncPlaywrightEngine(sync_playwright)


@dataclass
class _Session:
    engine: PlaywrightEngine
    scenario_digest: str
    request_digest: str
    started: float
    observed: bool = False


class _Cancelled(RuntimeError):
    pass


class PlaywrightBrowserEvaluationServices:
    """Developer-managed ephemeral browser sessions confined to one workspace."""

    def __init__(
        self,
        workspace_root: str | Path,
        artifact_store: ArtifactStore,
        cancellation: Cancellation,
        *,
        engine_factory: EngineFactory = default_playwright_engine_factory,
        id_factory: Callable[[str], str] = identifier,
        clock: Callable[[], Any] = now,
        monotonic: Callable[[], float] = time.monotonic,
        maximum_artifact_bytes: int = 8_000_000,
        maximum_response_bytes: int = 16_000_000,
    ) -> None:
        if maximum_artifact_bytes < 1 or maximum_response_bytes < 1:
            raise ValueError("browser byte limits must be positive")
        self._root = Path(workspace_root).resolve(strict=True)
        if not self._root.is_dir():
            raise ValueError("browser workspace root must be a directory")
        self._artifacts = artifact_store
        self._cancellation = cancellation
        self._engine_factory = engine_factory
        self._id_factory = id_factory
        self._clock = clock
        self._monotonic = monotonic
        self._maximum_artifact_bytes = maximum_artifact_bytes
        self._maximum_response_bytes = maximum_response_bytes
        self._sessions: dict[str, _Session] = {}
        self.observations: list[BrowserObservation] = []

    def new_id(self, prefix: str) -> str:
        return self._id_factory(prefix)

    def created_at(self) -> Any:
        return self._clock()

    def cancelled(self) -> bool:
        return self._cancellation.cancelled()

    def open_browser(self, scenario: BrowserScenario, request: EvaluationRequest) -> str:
        if self.cancelled():
            raise ValueError("browser evaluation was cancelled before launch")
        session_id = self.new_id("browser-session")
        if session_id in self._sessions:
            raise ValueError("browser engine reused a session identifier")
        engine = self._engine_factory()
        session = _Session(
            engine=engine,
            scenario_digest=versioned_digest(scenario),
            request_digest=request.content_digest or "",
            started=self._monotonic(),
        )
        self._sessions[session_id] = session
        try:
            origin = browser_origin(scenario.origin, origin_only=True)
            engine.open(lambda item: self._serve(origin, item))
        except BaseException:
            self.teardown_browser(session_id)
            raise
        return session_id

    def observe_browser(
        self,
        session_id: str,
        scenario: BrowserScenario,
        request: EvaluationRequest,
    ) -> BrowserObservation:
        try:
            session = self._sessions[session_id]
        except KeyError as error:
            raise ValueError("unknown browser session") from error
        if session.observed:
            raise ValueError("browser session was already observed")
        if session.scenario_digest != versioned_digest(scenario):
            raise ValueError("browser session belongs to another scenario")
        if session.request_digest != (request.content_digest or ""):
            raise ValueError("browser session belongs to another evaluation request")
        session.observed = True
        observation_id = self.new_id("browser-observation")
        actions_completed = 0
        artifacts: tuple[ArtifactDescriptor, ...] = ()
        failure: StableFailure | None = None
        status = "succeeded"
        try:
            for action in scenario.actions:
                self._check_active(session.started, scenario.timeout_seconds)
                self._perform_action(
                    session.engine,
                    action,
                    self._remaining(session.started, scenario.timeout_seconds),
                )
                actions_completed += 1
            payloads = self._capture_payloads(session, scenario)
            artifacts = self._put_artifacts(
                payloads,
                observation_id,
                session_id,
                scenario,
                request,
            )
        except _Cancelled:
            status = "cancelled"
            failure = StableFailure(
                code=StableFailureCode.CANCELLED,
                message="browser evaluation was cancelled",
            )
        except TimeoutError:
            status = "timed_out"
            failure = StableFailure(
                code=StableFailureCode.TIMEOUT,
                message="browser scenario timed out",
            )
        except BrowserRequestRejected:
            status = "failed"
            failure = StableFailure(
                code=StableFailureCode.NETWORK_BLOCKED,
                message="browser request was blocked by workspace confinement",
            )
        except Exception:
            status = "failed"
            failure = StableFailure(
                code=StableFailureCode.VERIFICATION_FAILED,
                message="browser scenario failed",
            )

        final_url = self._safe_final_url(session.engine)
        duration = min(max(0.0, self._monotonic() - session.started), scenario.timeout_seconds)
        observation = BrowserObservation(
            id=observation_id,
            run_id=request.run_id,
            created_at=self.created_at(),
            request_digest=request.content_digest or "",
            scenario_digest=session.scenario_digest,
            session_id=session_id,
            status=cast(Any, status),
            final_url=final_url,
            actions_completed=actions_completed,
            duration_seconds=duration,
            artifacts=artifacts,
            failure=failure,
        )
        self.observations.append(observation)
        return observation

    def teardown_browser(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        for callback in (
            session.engine.close_page,
            session.engine.close_context,
            session.engine.close_browser,
            session.engine.close_engine,
        ):
            with suppress(Exception):
                callback()

    def _serve(
        self,
        origin: tuple[str, str, int],
        request: PlaywrightRouteRequest,
    ) -> PlaywrightRouteResponse:
        if request.method.upper() not in {"GET", "HEAD"}:
            raise BrowserRequestRejected("browser request method is forbidden")
        if request.is_redirect:
            raise BrowserRequestRejected("browser redirects are forbidden")
        if request.is_background:
            raise BrowserRequestRejected("background browser requests are forbidden")
        if request.resource_type in {"eventsource", "serviceworker", "websocket"}:
            raise BrowserRequestRejected("browser resource type is forbidden")
        try:
            if browser_origin(request.url) != origin:
                raise BrowserRequestRejected("cross-origin browser request is forbidden")
        except ValueError:
            raise BrowserRequestRejected("external browser request is forbidden") from None

        raw_path = unquote(urlsplit(request.url).path, errors="strict")
        relative = PurePosixPath(raw_path.lstrip("/"))
        if "\x00" in raw_path or "\\" in raw_path or ".." in relative.parts:
            raise BrowserRequestRejected("browser path traversal is forbidden")
        candidate = self._root.joinpath(*relative.parts).resolve()
        if candidate.is_dir():
            candidate = (candidate / "index.html").resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError:
            raise BrowserRequestRejected("browser file escapes the workspace") from None
        if not candidate.is_file():
            raise BrowserRequestRejected("browser route is not a workspace file")
        if candidate.stat().st_size > self._maximum_response_bytes:
            raise BrowserRequestRejected("browser response exceeds its byte limit")
        body = candidate.read_bytes()
        if len(body) > self._maximum_response_bytes:
            raise BrowserRequestRejected("browser response exceeds its byte limit")
        media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        return PlaywrightRouteResponse(status=200, media_type=media_type, body=body)

    def _check_active(self, started: float, timeout_seconds: float) -> None:
        if self.cancelled():
            raise _Cancelled
        if self._monotonic() - started >= timeout_seconds:
            raise TimeoutError("browser scenario timed out")

    def _remaining(self, started: float, timeout_seconds: float) -> float:
        remaining = timeout_seconds - (self._monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("browser scenario timed out")
        return remaining

    @staticmethod
    def _perform_action(
        engine: PlaywrightEngine,
        action: BrowserAction,
        remaining: float,
    ) -> None:
        timeout = min(action.timeout_seconds, remaining)
        if action.kind == "navigate":
            assert action.url is not None
            engine.navigate(action.url, timeout)
        elif action.kind == "click":
            assert action.selector is not None
            engine.click(action.selector, timeout)
        elif action.kind == "fill":
            assert action.selector is not None and action.value is not None
            engine.fill(action.selector, action.value, timeout)
        else:
            raise ValueError("unsupported browser action")

    def _capture_payloads(
        self,
        session: _Session,
        scenario: BrowserScenario,
    ) -> tuple[tuple[str, str, str, bytes], ...]:
        payloads: list[tuple[str, str, str, bytes]] = []
        total = 0
        for capture in scenario.captures:
            self._check_active(session.started, scenario.timeout_seconds)
            remaining = self._remaining(session.started, scenario.timeout_seconds)
            if capture.kind == "screenshot":
                body = session.engine.screenshot(remaining)
                media_type = "image/png"
            elif capture.kind == "console":
                body = json.dumps(
                    session.engine.console(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
                media_type = "application/json"
            elif capture.kind == "dom":
                body = session.engine.dom(remaining).encode()
                media_type = "text/html"
            elif capture.kind == "accessibility":
                body = json.dumps(
                    session.engine.accessibility(remaining),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
                media_type = "application/json"
            else:
                raise ValueError("unsupported browser capture")
            total += len(body)
            if total > self._maximum_artifact_bytes:
                raise ValueError("browser artifacts exceed the configured byte limit")
            payloads.append((capture.id, capture.logical_kind, media_type, body))
        return tuple(payloads)

    def _put_artifacts(
        self,
        payloads: tuple[tuple[str, str, str, bytes], ...],
        observation_id: str,
        session_id: str,
        scenario: BrowserScenario,
        request: EvaluationRequest,
    ) -> tuple[ArtifactDescriptor, ...]:
        maximum = min(
            self._maximum_artifact_bytes,
            request.remaining_budget.remaining_artifact_bytes,
        )
        if sum(len(item[3]) for item in payloads) > maximum:
            raise ValueError("browser artifacts exceed the evaluation byte budget")
        scenario_digest = versioned_digest(scenario)
        return tuple(
            self._artifacts.put(
                BytesIO(body),
                ArtifactPutRequest(
                    id=self.new_id("browser-artifact-request"),
                    run_id=request.run_id,
                    created_at=self.created_at(),
                    media_type=media_type,
                    logical_kind=logical_kind,
                    producer_action_id=observation_id,
                    source=FrozenDict(
                        {
                            "request_digest": request.content_digest,
                            "scenario_digest": scenario_digest,
                            "session_id": session_id,
                            "capture_id": capture_id,
                        }
                    ),
                ),
            )
            for capture_id, logical_kind, media_type, body in payloads
        )

    @staticmethod
    def _safe_final_url(engine: PlaywrightEngine) -> str | None:
        try:
            value = engine.current_url()
            if value is None or value == "about:blank":
                return None
            browser_origin(value)
            return value
        except (RuntimeError, ValueError):
            return None
