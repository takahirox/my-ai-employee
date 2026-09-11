"""Investigate rejected work after reopening state, without calling real models."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from ai_employee.cli import projection
from ai_employee.container import ContainerModel
from ai_employee.diagnostics import CheckOutput, capture
from ai_employee.history import Journal, Stopped
from ai_employee.models import (
    Authority,
    Check,
    Clarification,
    Criterion,
    Finding,
    Plan,
    StagePolicy,
    Usage,
    Verification,
)

from .test_autonomous_runtime import OfflineModel, clarification, config, runtime
from .test_stage_contracts import RawModel


def reopened_cli(root: Path, run: str) -> dict:
    process = subprocess.run(
        [sys.executable, "-m", "ai_employee", "--state", str(root), "inspect", run],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def diagnostics(view: dict, kind: str) -> list[dict]:
    return [
        e
        for e in view["stage_diagnostics"]
        if "record" in e and json.loads(e["context"]["text"]).get("kind") == kind
    ]


def payload(event: dict):
    assert not event["record"]["truncated"]
    return json.loads(event["record"]["text"])


def test_rejected_review_and_proposal_survive_reopen_and_cli(tmp_path):
    class Reject(OfflineModel):
        def generate(self, policy, prompt, schema, *args, **kwargs):
            if schema is Verification and "proposal" in json.loads(prompt):
                return Verification(
                    summary="Criterion result lacks an output-path requirement",
                    findings=(
                        Finding(
                            criterion_id="review",
                            passed=False,
                            category="omitted_requirement",
                            evidence=(
                                "The proposal does not require result.txt. "
                                "Bearer private-fixture-token"
                            ),
                        ),
                    ),
                ), Usage(tokens=7)
            return super().generate(policy, prompt, schema, *args, **kwargs)

    model = Reject()
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={"clarification": StagePolicy(model="fixture", review="always", revisions=1)}
    )
    with pytest.raises(ValueError, match="CLARIFICATION_REJECTED"):
        engine.start("Write result", cfg, source)
    run = engine.journal.runs()[0]
    view = reopened_cli(tmp_path, run)
    assert view["status"] == "failed" and view["goal"] is None and model.workers == 0
    records = diagnostics(view, "model_response")
    assert len(records) == 4
    proposals = [e for e in records if e["stage"] == "clarification"]
    reviews = [e for e in records if e["stage"] == "clarification_review"]
    assert all(payload(e) == clarification().model_dump(mode="json") for e in proposals)
    assert all("lacks an output-path" in payload(e)["summary"] for e in reviews)
    assert all(
        "does not require result.txt" in payload(e)["findings"][0]["evidence"] for e in reviews
    )
    assert all(e["record"]["redactions"] == 1 for e in reviews)
    assert "private-fixture-token" not in json.dumps(view)
    assert all(json.loads(e["context"]["text"])["reservation"] for e in records)
    assert json.loads(reviews[0]["context"]["text"])["target"] == clarification().digest
    assert (
        projection(Journal(tmp_path / "history.db"), run)["stage_diagnostics"]
        == view["stage_diagnostics"]
    )
    assert all(e["authoritative"] is False for e in records)


def test_unaccepted_plan_and_exact_authority_failure_survive_reopen(tmp_path):
    class Unsupported(OfflineModel):
        def generate(self, policy, prompt, schema, *args, **kwargs):
            value, usage = super().generate(policy, prompt, schema, *args, **kwargs)
            if schema is Plan:
                value = value.model_copy(
                    update={
                        "tasks": (
                            value.tasks[0].model_copy(
                                update={
                                    "authority": Authority(operation_approval=True),
                                }
                            ),
                        )
                    }
                )
            return value, usage

    model = Unsupported()
    engine, source = runtime(tmp_path, model)
    with pytest.raises(Stopped, match="REQUIRED_AUTHORITY_BOUNDARY_UNAVAILABLE"):
        engine.start("Write result", config(), source)
    view = reopened_cli(tmp_path, engine.journal.runs()[0])
    assert view["plan"] is None and model.workers == 0
    plans = [e for e in diagnostics(view, "model_response") if e["stage"] == "planning"]
    plan = payload(plans[0])
    failure = diagnostics(view, "readiness_failed")[0]
    assert plan["tasks"][0]["authority"]["operation_approval"] is True
    assert payload(failure)["unsupported_fields"] == ["operation_approval"]
    assert json.loads(failure["context"]["text"])["task"] == "write"
    assert json.loads(failure["context"]["text"])["target"] == Plan.model_validate(plan).digest


def test_invalid_reference_details_and_each_repair_attempt_remain_observable(tmp_path):
    engine, source = runtime(tmp_path, RawModel("foreign_check", always=True))
    with pytest.raises(RuntimeError, match="OUTPUT_REPAIR_EXHAUSTED"):
        engine.start("Write result", config(), source)
    view = reopened_cli(tmp_path, engine.journal.runs()[0])
    records = diagnostics(view, "output_rejected")
    assert len(records) == 2
    assert all(payload(e)["path"] == "criteria.checks" for e in records)
    assert all(payload(e)["unregistered_checks"] == ["not-registered"] for e in records)
    assert len({json.loads(e["context"]["text"])["reservation"] for e in records}) == 2
    assert view["status"] == "failed"


def test_real_check_output_is_separate_from_acceptance_receipt(tmp_path):
    backend = ContainerModel(None)
    candidate = MagicMock()
    candidate.run_guarded.return_value = (
        1,
        b'{"api_key": "sensitive-fixture"}',
        b"AssertionError: expected 2 rows, got 1",
    )
    with patch.object(backend, "_candidate") as environment, patch.object(backend, "_native_probe"):
        environment.return_value.__enter__.return_value = candidate
        passed, output = backend.check(("python", "check.py"), tmp_path, 10, lambda: False)
    assert not passed and isinstance(output, CheckOutput)

    class Checks(OfflineModel):
        def generate(self, policy, prompt, schema, *args, **kwargs):
            if schema is Clarification:
                return clarification().model_copy(
                    update={
                        "criteria": (
                            Criterion(
                                id="result", description="result exists", checks=("row-count",)
                            ),
                        )
                    }
                ), Usage(tokens=0)
            return super().generate(policy, prompt, schema, *args, **kwargs)

        def check(self, *args):
            return passed, output

    (tmp_path / "runtime").mkdir()
    engine, source = runtime(tmp_path / "runtime", Checks())
    cfg = config(replans=0, task_attempts=1).model_copy(
        update={
            "checks": (Check(id="row-count", argv=("python", "check.py")),),
            "mandatory_checks": ("row-count",),
        }
    )
    with pytest.raises(ValueError):
        engine.start("Write result", cfg, source)
    view = reopened_cli(tmp_path / "runtime", engine.journal.runs()[0])
    record = diagnostics(view, "check_output")[0]
    assert payload(record)["exit_code"] == 1
    assert payload(record)["stderr"] == "AssertionError: expected 2 rows, got 1"
    assert "sensitive-fixture" not in json.dumps(view)
    receipts = [e["body"] for e in view["events"] if e["kind"] == "check_result"]
    assert receipts[0]["evidence"] == output.digest
    assert json.loads(record["context"]["text"])["check"] == "row-count"


def test_capacity_redaction_and_late_diagnostics_are_explicit(tmp_path):
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config())
    journal.stop(run, "fixture")
    with patch("ai_employee.history.RECORD_BYTES", 64), patch("ai_employee.history.RUN_BYTES", 128):
        journal.diagnostic(run, "review", {"summary": "x" * 1000, "password": "private-fixture"})
        journal.diagnostic(run, "review", {"summary": "after capacity"}, kind="model_response")
    records = [e["body"] for e in Journal(journal.path).events(run) if e["kind"] == "diagnostic"]
    assert records[0]["record"]["truncated"] and records[0]["record"]["redactions"] == 1
    assert records[1]["capacity_exhausted"] and records[1]["record"]["stored_bytes"] == 0
    assert "private-fixture" not in json.dumps(records)
    assert not any(e["kind"] == "accepted" for e in journal.events(run))
    assert journal.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "value",
    [
        {"authorization": "Bearer private-fixture"},
        {"nested": {"refresh_token": "private-fixture"}},
        'api_key="private-fixture"',
        '{"password": "private-fixture"}',
        'password="two words private-fixture"',
        "token='two words private-fixture'",
        "Bearer private-fixture",
        "-----BEGIN PRIVATE KEY-----\nprivate-fixture\n-----END PRIVATE KEY-----",
    ],
)
def test_credential_patterns_are_redacted_before_truncation(value):
    record = capture(value)
    assert "private-fixture" not in record["text"]
    assert record["redactions"] > 0


@pytest.mark.parametrize("fault", ["foreign_local", "malformed"])
def test_native_schema_failure_keeps_recognized_fields_and_explains_omission(tmp_path, fault):
    engine, source = runtime(tmp_path, RawModel(fault))
    run = engine.start("Write result", config(), source)
    view = reopened_cli(tmp_path, run)
    detail = payload(diagnostics(view, "output_rejected")[0])
    assert detail["errors"]
    if fault == "foreign_local":
        assert detail["payload"]["requirements"][0]["criteria"] == ["foreign"]
        assert detail["omitted_unknown_fields"] == 0
    else:
        assert detail["omitted_unknown_fields"] == 1
        assert "secret-must-not-be-recorded" not in json.dumps(view)
    assert view["status"] == "completed"
    before = len(engine.journal.events(run))
    engine.execute(run)
    assert len(engine.journal.events(run)) == before


def test_mandatory_check_omission_identifies_missing_reference(tmp_path):
    engine, source = runtime(tmp_path, OfflineModel())
    cfg = config().model_copy(
        update={
            "checks": (Check(id="required", argv=("true",)),),
            "mandatory_checks": ("required",),
        }
    )
    with pytest.raises(RuntimeError, match="OUTPUT_REPAIR_EXHAUSTED"):
        engine.start("Write result", cfg, source)
    view = reopened_cli(tmp_path, engine.journal.runs()[0])
    detail = payload(diagnostics(view, "output_rejected")[0])
    assert detail["path"] == "criteria.checks"
    assert detail["mandatory_checks"] == ["required"]


def test_adapter_validation_error_before_return_preserves_usage_and_repairs(tmp_path):
    class DirectError(OfflineModel):
        failed = False

        def generate(self, policy, prompt, schema, *args, **kwargs):
            if not self.failed:
                self.failed = True
                kwargs["observation"]({"event": "usage_observed", "tokens": 17})
                # Alternative adapters may raise Pydantic errors before returning a result.
                schema.model_validate({})
            return super().generate(policy, prompt, schema, *args, **kwargs)

    with pytest.raises(ValidationError):
        Clarification.model_validate({})
    engine, source = runtime(tmp_path, DirectError())
    run = engine.start("Write result", config(), source)
    view = reopened_cli(tmp_path, run)
    detail = payload(diagnostics(view, "output_rejected")[0])
    assert detail["non_object_payload_omitted"] and detail["payload"] is None
    assert view["status"] == "completed"
    settlements = [e["body"] for e in view["events"] if e["kind"] == "settled"]
    assert settlements[0]["usage"]["tokens"] == 17
