"""Execute pinned upstream public artifacts, without models, graders or networking.

CI fetches only three public source files before pytest. Local runs may point
FLEET_PUBLIC_CONTRACT_ROOT at an explicitly prepared copy of those same bytes.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_employee.benchmark import Request, cleanup, public_check, run

from .test_autonomous_benchmark import BenchmarkModel, request
from .test_autonomous_runtime import config

HASHES = {
    "smoke.py": "c736b7023939e7d3a723deb3559ac63823de65eb93353145feddc3a3ad196bee",
    "execution.py": "1cbea6bd130122d241db92cde4cc5f0b4aeaeaefee201884d78eeb33c4984463",
    "connected_agent.py": "bc80ad380c3cdcf7081afffcc125f8ee686217d9635bd6f97c82c37bb637f0e5",
}


@pytest.fixture
def public_sources() -> dict[str, str]:
    root = os.environ.get("FLEET_PUBLIC_CONTRACT_ROOT")
    if root is None:
        pytest.skip("pinned public artifacts not supplied; CI supplies them")
    result = {}
    for name, expected in HASHES.items():
        data = (Path(root) / "src/pocket_bench" / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected, "public contract version changed"
        result[name] = data.decode()
    return result


def test_real_public_smoke_has_file_context_and_never_executes_declared_script(
    tmp_path: Path,
    public_sources: dict[str, str],
):
    (tmp_path / "src").mkdir()
    (tmp_path / "output").mkdir()
    (tmp_path / "src/program.py").write_text('raise RuntimeError("must not execute")\n')
    (tmp_path / "output/execute.json").write_text('{"script":"src/program.py"}')
    check = public_check({k: public_sources[k] for k in ("smoke.py", "execution.py")})
    result = subprocess.run(check.argv, cwd=tmp_path, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    (tmp_path / "output/execute.json").write_text('{"script":"../escape.py"}')
    result = subprocess.run(check.argv, cwd=tmp_path, capture_output=True, timeout=10)
    assert result.returncode != 0


def test_actual_upstream_request_expression_runs_and_cleanup_is_idempotent(
    tmp_path: Path,
    public_sources: dict[str, str],
):
    fixture = request(tmp_path)
    for name in ("smoke.py", "execution.py"):
        (fixture.public_checks / name).write_text(public_sources[name])
    (fixture.workspace / "src/program.py").write_text("pass\n")
    # Evaluate only the hash-pinned public request-construction expression, not
    # upstream adapters, setup, grading or model execution.
    module = ast.parse(public_sources["connected_agent.py"])
    expression = next(
        n.value
        for n in ast.walk(module)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "request" for t in n.targets)
        and isinstance(n.value, ast.Dict)
    )
    value = eval(
        compile(ast.Expression(expression), "<pinned-public-request>", "eval"),
        {
            "PROTOCOL": "pocket-agent-v1",
            "instruction": "Write result",
            "workspace": fixture.workspace,
            "control": tmp_path,
            "checks": fixture.public_checks,
            "WRITABLE_ROOTS": ("src", "output"),
            "deadline": 60,
            "time": SimpleNamespace(monotonic=lambda: 0),
            "self": SimpleNamespace(
                model_name="fixed-model",
                effort="medium",
                profile={"settings": {"config": config().model_dump(mode="json")}},
            ),
        },
    )
    parsed = Request.model_validate(value)
    assert parsed.configured().worker.model == "fixed-model"
    assert parsed.configured().verification.effort == "medium"

    class RealCheck(BenchmarkModel):
        def check(self, argv, workspace, timeout, cancelled):
            result = subprocess.run(argv, cwd=workspace, capture_output=True, timeout=timeout)
            return result.returncode == 0, "public-check-exit-" + str(result.returncode)

    adapter = RealCheck()
    response = run(parsed, adapter)
    assert response["details"]["exported"]
    value["operation"] = "cleanup"
    value["seconds"] = 0
    cleaned = Request.model_validate(value)
    assert cleanup(cleaned, adapter)["outcome"] == "cleaned"
    assert cleanup(cleaned, adapter)["outcome"] == "cleaned"
    assert (tmp_path / "fleet-autonomous/history.db").is_file()
    assert json.loads(json.dumps(response))["protocol"] == "pocket-agent-v1"
