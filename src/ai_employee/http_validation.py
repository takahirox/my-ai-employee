"""Deterministic public HTTP fixtures for offline stdlib Python candidate checks.

This is a behavioral test double, not an adversarial in-process security boundary.
The outer native sandbox enforces offline execution. Unmatched requests fail closed.
"""

from __future__ import annotations

import http.client
import io
import json
import runpy
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def validate_fixture(value: dict[str, Any]) -> None:
    if set(value) != {"steps"} or not isinstance(value["steps"], list):
        raise ValueError("HTTP fixture requires steps")
    if not 1 <= len(value["steps"]) <= 100 or len(json.dumps(value)) > 64_000:
        raise ValueError("HTTP fixture exceeds its bound")
    for step in value["steps"]:
        if not isinstance(step, dict) or set(step) - {
            "method",
            "path",
            "request_json",
            "status",
            "response_json",
            "optional",
        }:
            raise ValueError("invalid HTTP fixture step")
        if (
            step.get("method") not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"}
            or not isinstance(step.get("path"), str)
            or not step["path"].startswith("/")
            or type(step.get("status")) is not int
            or not 100 <= step["status"] <= 599
            or "response_json" not in step
            or type(step.get("optional", False)) is not bool
        ):
            raise ValueError("invalid HTTP fixture request or response")


class _Socket:
    def __init__(self, payload: bytes):
        self.payload = payload

    def makefile(self, *_args: Any, **_kwargs: Any) -> io.BytesIO:
        return io.BytesIO(self.payload)


class HttpScenario:
    def __init__(self, fixture: dict[str, Any]):
        validate_fixture(fixture)
        self.steps = fixture["steps"]
        self.index = 0
        self.failure: str | None = None

    def response(self, method: str, url: str, body: Any) -> http.client.HTTPResponse:
        if hasattr(body, "read"):
            body = body.read(64_001)
        parsed = urlsplit(url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        request_json = json.loads(body) if body else None
        while self.index < len(self.steps):
            step = self.steps[self.index]
            matches = (
                method == step["method"]
                and path == step["path"]
                and ("request_json" not in step or step["request_json"] == request_json)
            )
            if matches:
                self.index += 1
                payload = json.dumps(step["response_json"]).encode()
                status = step["status"]
                reason = http.client.responses.get(status, "Fixture")
                wire = (
                    f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
                ).encode() + payload
                response = http.client.HTTPResponse(_Socket(wire))  # type: ignore[arg-type]
                response.begin()
                return response
            if not step.get("optional", False):
                break
            self.index += 1
        self.failure = f"HTTP protocol mismatch at step {self.index + 1}: {method} {path}"
        raise AssertionError(self.failure)

    def finish(self) -> None:
        if self.failure:
            raise AssertionError(self.failure)
        if any(not step.get("optional", False) for step in self.steps[self.index :]):
            raise AssertionError(f"HTTP protocol incomplete at step {self.index + 1}")


def execute(fixture: dict[str, Any], script: Path) -> None:
    scenario = HttpScenario(fixture)
    responses: dict[int, http.client.HTTPResponse] = {}
    original_request = http.client.HTTPConnection.request
    original_response = http.client.HTTPConnection.getresponse

    def request(
        connection: Any, method: str, url: str, body: Any = None, *_args: Any, **_kwargs: Any
    ) -> None:
        responses[id(connection)] = scenario.response(method, url, body)

    def getresponse(connection: Any) -> http.client.HTTPResponse:
        return responses.pop(id(connection))

    http.client.HTTPConnection.request = request  # type: ignore[assignment]
    http.client.HTTPConnection.getresponse = getresponse  # type: ignore[method-assign,assignment]
    try:
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit as error:
            if error.code not in (None, 0):
                raise
        scenario.finish()
    finally:
        http.client.HTTPConnection.request = original_request  # type: ignore[method-assign]
        http.client.HTTPConnection.getresponse = original_response  # type: ignore[method-assign]


def main() -> None:
    execute(json.loads(Path(sys.argv[1]).read_bytes()), Path(sys.argv[2]))


if __name__ == "__main__":
    main()
