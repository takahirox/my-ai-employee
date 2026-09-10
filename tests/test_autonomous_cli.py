"""Public operator commands and the read-only Inspector use the current runtime."""

from __future__ import annotations

import json
import os
import threading
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_employee.cli import main, projection
from ai_employee.inspector import create_server
from ai_employee.models import RunConfig

from .test_autonomous_runtime import OfflineModel, config, runtime


def test_init_writes_current_schema_and_refuses_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "run.json"
    args = [
        "init",
        "--model",
        "fixture-model",
        "--image",
        "sha256:" + "1" * 64,
        "--auth-file",
        str(tmp_path / "delegated-fixture.json"),
        "--output",
        str(output),
    ]
    assert main(args) == 0
    configured = RunConfig.model_validate_json(output.read_text())
    assert configured.clarification.review == "always"
    assert configured.isolation is not None
    original = output.read_bytes()
    assert main(args) == 2
    assert output.read_bytes() == original
    assert "error" in json.loads(capsys.readouterr().out.splitlines()[-1])


def test_inspection_and_publication_do_not_invoke_models(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine, source = runtime(tmp_path, OfflineModel())
    run = engine.start("Write result", config(), source)
    with patch(
        "ai_employee.container.ContainerModel.generate", side_effect=AssertionError("model called")
    ):
        assert main(["--state", str(tmp_path), "inspect", run]) == 0
        inspected = json.loads(capsys.readouterr().out)
        assert inspected["status"] == "completed"
        assert inspected["budget"]["measured_usage"]["tokens"] > 0
        assert (
            main(
                [
                    "--state",
                    str(tmp_path),
                    "promote",
                    run,
                    "--destination",
                    str(tmp_path / "published"),
                ]
            )
            == 0
        )
    assert (tmp_path / "published/result.txt").read_text() == "correct"


def test_parallel_wait_projection_uses_attempt_identity(tmp_path: Path) -> None:
    engine, source = runtime(tmp_path, OfflineModel())
    run = engine.prepare("Write result", config(), source)
    engine.journal.append(run, "approval_wait", attempt="first")
    engine.journal.append(run, "approval_wait", attempt="second")
    engine.journal.append(run, "authority_applied", attempt="second")
    assert projection(engine.journal, run)["status"] == "paused_for_approval"
    engine.journal.append(run, "authority_applied", attempt="first")
    assert projection(engine.journal, run)["status"] == "running"


@pytest.mark.skipif(
    os.environ.get("FLEET_TEST_INSPECTOR_HTTP") != "1", reason="explicit loopback HTTP test opt-in"
)
def test_inspector_http_requires_token_and_provides_no_mutation_route(tmp_path: Path) -> None:
    engine, source = runtime(tmp_path, OfflineModel())
    engine.prepare("Private fixture goal", config(), source)
    server, token = create_server(engine.journal)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request("GET", "/api/runs")
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request("GET", "/api/runs", headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())[0]["title"] == "Private fixture goal"
        connection.request(
            "POST", "/api/runs", body="{}", headers={"Authorization": "Bearer " + token}
        )
        response = connection.getresponse()
        assert response.status == 501
        response.read()
        connection.request(
            "GET",
            "/api/runs",
            headers={"Host": "untrusted.example", "Authorization": "Bearer " + token},
        )
        response = connection.getresponse()
        assert response.status == 403
        response.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
