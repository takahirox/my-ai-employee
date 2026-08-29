"""Strict immutable contracts for confined mediated browser evaluation."""

from __future__ import annotations

import math
from datetime import datetime
from ipaddress import ip_address
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .base import Digest, Identifier
from .v2 import (
    ArtifactDescriptor,
    DigestedRecordV2,
    SchemaModelV2,
    StableFailure,
    StableFailureCode,
)

if TYPE_CHECKING:
    from .evaluation import EvaluationRequest


BROWSER_EVALUATOR_ID = "browser.playwright"


def browser_origin(value: str, *, origin_only: bool = False) -> tuple[str, str, int]:
    parts = urlsplit(value)
    if (
        not value
        or any(ord(character) < 0x20 for character in value)
        or parts.scheme not in {"http", "https"}
        or parts.hostname is None
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("browser URL requires credential-free http or https")
    host = parts.hostname
    if host == "localhost":
        normalized_host = host
    else:
        try:
            address = ip_address(host)
        except ValueError as error:
            raise ValueError("browser URL host must be localhost or a loopback IP") from error
        if not address.is_loopback:
            raise ValueError("browser URL host must be loopback-only")
        normalized_host = address.compressed
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("browser URL has an invalid port") from error
    if origin_only and (parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise ValueError("browser origin cannot contain a path, query, or fragment")
    return parts.scheme, normalized_host, port or (443 if parts.scheme == "https" else 80)


class BrowserAction(SchemaModelV2):
    schema_name: ClassVar[str] = "browser_action"
    kind: Literal["navigate", "click", "fill"]
    url: str | None = Field(default=None, max_length=4_096)
    selector: str | None = Field(default=None, min_length=1, max_length=1_000)
    value: str | None = Field(default=None, max_length=10_000)
    timeout_seconds: float = Field(default=10.0, gt=0)

    @model_validator(mode="after")
    def _shape_is_exact(self) -> Self:
        valid = {
            "navigate": self.url is not None and self.selector is None and self.value is None,
            "click": self.url is None and self.selector is not None and self.value is None,
            "fill": self.url is None and self.selector is not None and self.value is not None,
        }[self.kind]
        if not valid:
            raise ValueError(f"browser {self.kind} action has invalid fields")
        if self.url is not None:
            browser_origin(self.url)
        if self.selector is not None and (not self.selector.strip() or "\x00" in self.selector):
            raise ValueError("browser selector must be non-blank and NUL-free")
        if self.value is not None and "\x00" in self.value:
            raise ValueError("browser fill value must be NUL-free")
        if not math.isfinite(self.timeout_seconds):
            raise ValueError("browser action timeout must be finite")
        return self


class BrowserCapture(SchemaModelV2):
    schema_name: ClassVar[str] = "browser_capture"
    id: Identifier
    kind: Literal["screenshot", "console", "dom", "accessibility"]
    logical_kind: Literal[
        "browser_screenshot", "browser_console", "browser_dom", "browser_accessibility"
    ]

    @model_validator(mode="after")
    def _kind_matches_observation(self) -> Self:
        expected = {
            "screenshot": "browser_screenshot",
            "console": "browser_console",
            "dom": "browser_dom",
            "accessibility": "browser_accessibility",
        }[self.kind]
        if self.logical_kind != expected:
            raise ValueError("browser capture kind and observation kind disagree")
        return self


class BrowserScenario(SchemaModelV2):
    schema_name: ClassVar[str] = "browser_scenario"
    origin: str = Field(max_length=4_096)
    actions: tuple[BrowserAction, ...] = Field(min_length=1)
    captures: tuple[BrowserCapture, ...] = Field(min_length=1)
    timeout_seconds: float = Field(default=30.0, gt=0)
    profile_mode: Literal["isolated_ephemeral"] = "isolated_ephemeral"
    inherit_credentials: Literal[False] = False
    external_request_policy: Literal["deny"] = "deny"

    @model_validator(mode="after")
    def _scenario_is_confined(self) -> Self:
        origin = browser_origin(self.origin, origin_only=True)
        if self.actions[0].kind != "navigate":
            raise ValueError("browser scenario must begin with navigation")
        if any(
            item.url is not None and browser_origin(item.url) != origin for item in self.actions
        ):
            raise ValueError("browser navigation must remain on the exact scenario origin")
        capture_ids = tuple(item.id for item in self.captures)
        capture_kinds = tuple(item.logical_kind for item in self.captures)
        if len(capture_ids) != len(set(capture_ids)) or len(capture_kinds) != len(
            set(capture_kinds)
        ):
            raise ValueError("browser captures must have unique IDs and observation kinds")
        if not math.isfinite(self.timeout_seconds):
            raise ValueError("browser scenario timeout must be finite")
        return self


class BrowserObservation(DigestedRecordV2):
    schema_name: ClassVar[str] = "browser_observation"
    request_digest: Digest
    scenario_digest: Digest
    session_id: Identifier
    status: Literal["succeeded", "failed", "cancelled", "timed_out"]
    final_url: str | None = Field(default=None, max_length=4_096)
    actions_completed: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    artifacts: tuple[ArtifactDescriptor, ...] = ()
    failure: StableFailure | None = None

    @model_validator(mode="after")
    def _observation_is_consistent(self) -> Self:
        if self.final_url is not None:
            browser_origin(self.final_url)
        if not math.isfinite(self.duration_seconds):
            raise ValueError("browser observation duration must be finite")
        if (self.status == "succeeded") == (self.failure is not None):
            raise ValueError("browser observation status and failure disagree")
        expected_code = {
            "cancelled": StableFailureCode.CANCELLED,
            "timed_out": StableFailureCode.TIMEOUT,
        }.get(self.status)
        actual_code = None if self.failure is None else self.failure.code
        if expected_code is not None and actual_code is not expected_code:
            raise ValueError("browser terminal status has the wrong failure code")
        if any(item.run_id != self.run_id for item in self.artifacts):
            raise ValueError("browser artifact belongs to another run")
        digests = tuple(item.content_digest for item in self.artifacts)
        if len(digests) != len(set(digests)):
            raise ValueError("browser artifact descriptors must be unique")
        return self


class BrowserEvaluationServices(Protocol):
    """Fakeable boundary that owns cancellation, network denial, and ephemeral profiles."""

    def new_id(self, prefix: str) -> Identifier: ...

    def created_at(self) -> datetime: ...

    def cancelled(self) -> bool: ...

    def open_browser(self, scenario: BrowserScenario, request: EvaluationRequest) -> Identifier: ...

    def observe_browser(
        self,
        session_id: Identifier,
        scenario: BrowserScenario,
        request: EvaluationRequest,
    ) -> BrowserObservation: ...

    def teardown_browser(self, session_id: Identifier) -> None: ...
