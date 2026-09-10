"""The benchmark connection uses the ordinary Goal/verification/promotion pipeline."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ai_employee.benchmark import Request, run
from ai_employee.models import (
    Authority,
    Clarification,
    Criterion,
    Finding,
    StagePolicy,
    Usage,
    Verification,
    WorkerResult,
)
from ai_employee.native import T

from .test_autonomous_runtime import OfflineModel, clarification, config


class BenchmarkModel(OfflineModel):
    def __init__(self, *, check_passes: bool = True, changes_input: bool = False) -> None:
        super().__init__()
        self.check_passes = check_passes
        self.changes_input = changes_input

    def generate(
        self,
        policy: StagePolicy,
        prompt: str,
        schema: type[T],
        workspace: Path,
        authority: Authority,
        timeout: float,
        cancelled: Callable[[], bool],
        observer: Callable[[float, int], None] | None = None,
        observation: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[T, Usage]:
        body = json.loads(prompt)
        if schema is Clarification:
            result = clarification().model_copy(
                update={
                    "criteria": (
                        Criterion(
                            id="result",
                            description="result exists",
                            checks=tuple(body["mandatory_checks"]),
                        ),
                    )
                }
            )
        elif schema is WorkerResult:
            (workspace / "src").mkdir(exist_ok=True)
            (workspace / "src/result.txt").write_text("correct")
            if self.changes_input:
                (workspace / "input/source.txt").write_text("unauthorized change")
            result = WorkerResult(status="completed", summary="actual output written")
        elif schema is Verification:
            result = Verification(
                findings=tuple(
                    Finding(criterion_id=item["id"], passed=True, evidence="fixture inspection")
                    for item in body["criteria"]
                ),
                summary="fixture verification",
            )
        else:
            return super().generate(
                policy, prompt, schema, workspace, authority, timeout, cancelled
            )
        return schema.model_validate(result.model_dump()), Usage(tokens=10, cost=0)

    def check(
        self, argv: tuple[str, ...], workspace: Path, timeout: float, cancelled: Callable[[], bool]
    ) -> tuple[bool, str]:
        assert "public smoke fixture" in argv[-1]
        return self.check_passes, "fixture-check-evidence"


def request(tmp_path: Path) -> Request:
    workspace = tmp_path / "workspace"
    (workspace / "input").mkdir(parents=True)
    (workspace / "input/source.txt").write_text("public input")
    (workspace / "src").mkdir()
    (workspace / "output").mkdir()
    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / "smoke.py").write_text("# public smoke fixture")
    (checks / "execution.py").write_text("# public helper fixture")
    return Request(
        protocol="pocket-agent-v1",
        operation="run",
        control_dir=tmp_path,
        workspace=workspace,
        public_checks=checks,
        instruction="Write result",
        seconds=60,
        config=config(replans=0, task_attempts=1),
    )


def test_benchmark_exports_actual_verified_files(tmp_path: Path) -> None:
    fixture = request(tmp_path)
    response = run(fixture, BenchmarkModel())
    assert response["details"]["exported"] is True
    assert (fixture.workspace / "src/result.txt").read_text() == "correct"
    assert (fixture.workspace / "input/source.txt").read_text() == "public input"


@pytest.mark.parametrize("options", [{"check_passes": False}, {"changes_input": True}])
def test_benchmark_does_not_export_rejected_result(
    tmp_path: Path, options: dict[str, bool]
) -> None:
    fixture = request(tmp_path)
    response = run(fixture, BenchmarkModel(**options))
    assert response["details"]["exported"] is False
    assert not (fixture.workspace / "src/result.txt").exists()
    assert (fixture.workspace / "input/source.txt").read_text() == "public input"


def test_benchmark_propagates_usage_limit_without_retry(tmp_path: Path) -> None:
    fixture = request(tmp_path)
    model = OfflineModel(quota=True)
    response = run(fixture, model)
    assert response["outcome"] == "usage_limit"
    assert model.calls == ["Clarification"]
