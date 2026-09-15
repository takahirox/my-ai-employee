"""Provider usage reaches durable, replay-safe public accounting without model access."""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from ai_employee.cli import projection
from ai_employee.container import ContainerModel
from ai_employee.history import Journal, Stopped
from ai_employee.isolated_worker import IsolatedWorkerProfile
from ai_employee.models import Clarification, StagePolicy, Usage
from ai_employee.native import decode_response, measured_usage
from ai_employee.stage_contracts import OutputViolation

from .test_autonomous_runtime import OfflineModel, config, runtime
from .test_stage_contracts import stream

RAW = {
    "input_tokens": 100,
    "output_tokens": 20,
    "cached_input_tokens": 60,
    "reasoning_output_tokens": 12,
    "cache_creation_input_tokens": 0,
}
USAGE = measured_usage(RAW)


def details(journal, run):
    return projection(journal, run)["budget"]["usage_details"]


class NativeUsageModel(OfflineModel):
    """Real native observation/decoding, with only process/container I/O replaced."""

    def generate(self, policy, prompt, schema, workspace, authority, timeout, cancelled, **kw):
        result, _ = super().generate(
            policy, prompt, schema, workspace, authority, timeout, cancelled
        )
        profile = IsolatedWorkerProfile(
            image="sha256:" + "a" * 64, auth_file=str(workspace / "fixture-auth")
        )
        native = ContainerModel(profile)
        candidate = MagicMock(profile=profile, deadline=None, proxy=None)
        candidate.name = "fixture"
        output = stream(result.model_dump()).splitlines()
        # The native stream reports usage snapshots, not deltas. Repeated identical
        # observations plus the final return must not triple the measured count.
        output[-1] = json.dumps({"type": "turn.completed", "usage": RAW})
        output.append(output[-1])

        def guarded(*args, **kwargs):
            for line in output:
                kwargs["observe"](json.loads(line))
            return 0, "\n".join(output).encode(), b""

        candidate.run_guarded.side_effect = guarded

        @contextmanager
        def owned(*args, **kwargs):
            yield candidate

        with (
            patch.object(native, "_candidate", owned),
            patch.object(native, "_native_probe"),
            patch.object(native, "_copy_workspace"),
        ):
            return native.generate(
                policy, prompt, schema, workspace, authority, timeout, cancelled, **kw
            )


def test_native_to_public_usage_survives_restart_cleanup_and_replay(tmp_path):
    model = NativeUsageModel()
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write result", config(), source)
    before = details(engine.journal, run)
    assert len(before["invocations"]) == 5
    assert before["total"]["tokens"] == {"value": 600, "observed_sum": 600, "complete": True}
    assert before["total"]["cached_input_tokens"]["value"] == 300
    for call in before["invocations"]:
        assert call["usage"] == USAGE.model_dump()
        assert call["usage"]["input_tokens"] - call["usage"]["cached_input_tokens"] == 40
        assert call["backend"] == "codex"
        assert call["model"] == config().worker.model
        assert call["effort"] == config().worker.effort
    assert sum(v["tokens"]["value"] for v in before["stages"].values()) == 600
    engine.execute(run)
    engine.cleanup(run)
    assert details(Journal(tmp_path / "history.db"), run) == before


@pytest.mark.parametrize("failure", ["cleanup", "cancel", "output", "transport"])
def test_observed_breakdown_survives_each_failed_boundary(tmp_path, failure):
    class Failed(OfflineModel):
        def generate(self, *args, observation=None, **kw):
            observation({"event": "usage_observed", **USAGE.model_dump()})
            if failure == "cancel":
                raise Stopped("CANCELLED")
            if failure == "output":
                raise OutputViolation("INVALID_OUTPUT", Usage(tokens=120))
            if failure == "transport":
                raise ConnectionError("offline failure")
            raise RuntimeError("cleanup unconfirmed")

    engine, source = runtime(tmp_path, Failed())
    run = engine.prepare("Write result", config(), source)
    with pytest.raises((Stopped, RuntimeError, ConnectionError)):
        engine.execute(run)
    view = details(engine.journal, run)
    assert view["invocations"]
    assert all(c["usage"] == USAGE.model_dump() for c in view["invocations"])
    assert view["total"]["tokens"]["value"] == 120 * len(view["invocations"])


def test_missing_partial_and_invalid_fields_are_not_zero():
    u = measured_usage(
        {
            "input_tokens": 100,
            "cached_input_tokens": 60,
            "output_tokens": True,
            "reasoning_output_tokens": -1,
        }
    )
    assert u.input_tokens == 100 and u.cached_input_tokens == 60
    assert u.tokens is None and u.output_tokens is None
    assert u.reasoning_output_tokens is None and u.cache_creation_input_tokens is None
    assert measured_usage(None) == Usage()


def test_decode_violation_keeps_partial_breakdown():
    output = stream({}).splitlines()
    output[-1] = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100}})
    with pytest.raises(OutputViolation) as error:
        decode_response("\n".join(output), Clarification)
    assert error.value.usage.input_tokens == 100
    assert error.value.usage.tokens is None


def test_legacy_settlement_remains_idempotent_and_unknown(tmp_path):
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config())
    reservation, _ = journal.reserve(run, "worker")
    # Write a genuinely old-shaped record, without newly added optional fields.
    with journal.connect() as db:
        db.execute("UPDATE reservations SET settled=1,tokens=7 WHERE id=?", (reservation,))
        journal._event(
            db,
            run,
            "settled",
            {"id": reservation, "seconds": 1, "usage": {"tokens": 7, "cost": None}},
        )
    journal.settle(run, reservation, 1, Usage(tokens=7))
    view = details(Journal(tmp_path / "history.db"), run)
    assert view["total"]["tokens"]["value"] == 7
    assert view["total"]["input_tokens"] == {"value": None, "observed_sum": 0, "complete": False}
    assert view["invocations"][0]["model"] is None
    with pytest.raises(ValueError, match="CONFLICTING_USAGE_DELIVERY"):
        journal.settle(run, reservation, 1, Usage(tokens=7, input_tokens=6, output_tokens=1))


def test_crash_keeps_observation_without_claiming_final_usage(tmp_path):
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config(tokens=1000, reservation_tokens=200))
    reservation, _ = journal.reserve(
        run, "worker", policy=StagePolicy(model="actual", effort="high")
    )
    journal.append(
        run,
        "worker_observation",
        reservation=reservation,
        stage="worker",
        observation={"event": "usage_observed", **USAGE.model_dump()},
    )
    journal = Journal(tmp_path / "history.db")
    journal.recover_reservations(run)
    view = details(journal, run)
    assert view["invocations"][0]["usage"] == USAGE.model_dump()
    assert view["invocations"][0]["model"] == "actual"
    assert view["invocations"][0]["recovered"]
    assert view["total"]["tokens"] == {"value": None, "observed_sum": 120, "complete": False}
    # An observation isn't a complete final bill or permission to release a reservation.
    assert journal.budget(run)["admission_charges"]["tokens"] == 200
    assert journal.budget(run)["measured_usage"]["tokens"] is None


def test_retry_counts_distinct_calls_and_preserves_selected_policy(tmp_path):
    class Retry(OfflineModel):
        failed = False

        def generate(self, *args, observation=None, **kwargs):
            observation({"event": "usage_observed", **USAGE.model_dump()})
            if not self.failed:
                self.failed = True
                raise ConnectionError("temporary")
            result, _ = super().generate(*args, **kwargs)
            return result, USAGE

    model = Retry()
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={
            "clarification": StagePolicy(
                model="fixture-sol",
                effort="high",
                transport_retries=1,
            ),
            "planning": StagePolicy(model="fixture-astra", effort="medium"),
        }
    )
    run = engine.start("Write result", cfg, source)
    view = details(engine.journal, run)
    calls = view["invocations"]
    assert len(calls) == 6
    assert view["total"]["tokens"]["value"] == 720
    assert [(c["model"], c["effort"]) for c in calls if c["stage"] == "clarification"] == [
        ("fixture-sol", "high"),
        ("fixture-sol", "high"),
    ]
    assert [(c["model"], c["effort"]) for c in calls if c["stage"] == "planning"] == [
        ("fixture-astra", "medium")
    ]


def test_changed_final_snapshot_does_not_inherit_stale_breakdown():
    merged = Usage(tokens=200).prefer(USAGE)
    assert merged.tokens == 200
    assert merged.input_tokens is None and merged.cached_input_tokens is None
    assert Usage(tokens=120).prefer(USAGE) == USAGE


def test_invalid_subsets_remain_unknown_without_losing_valid_totals():
    usage = measured_usage({**RAW, "cached_input_tokens": 101, "reasoning_output_tokens": 21})
    assert usage.tokens == 120
    assert usage.cached_input_tokens is None
    assert usage.reasoning_output_tokens is None


def test_aggregate_marks_partial_input_without_discarding_known_calls(tmp_path):
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config())
    first, _ = journal.reserve(run, "worker")
    journal.settle(run, first, 1, USAGE)
    second, _ = journal.reserve(run, "worker")
    journal.settle(run, second, 1, Usage(tokens=10))
    total = details(journal, run)["total"]
    assert total["tokens"]["value"] == 130
    assert total["input_tokens"] == {"value": None, "observed_sum": 100, "complete": False}


@pytest.mark.parametrize("repair", [False, True])
def test_review_and_repair_usage_retains_actual_model_choice(tmp_path, repair):
    from .test_autonomous_stage_policy import ReviewModel

    class MeasuredReview(ReviewModel):
        def generate(self, *args, **kwargs):
            result, _ = super().generate(*args, **kwargs)
            return result, USAGE

    model = MeasuredReview(repair=repair)
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={
            "clarification": StagePolicy(
                model="clarifier", review="always", reviewer_model="reviewer", reviewer_effort="max"
            ),
            "worker_escalations": (StagePolicy(model="stronger", effort="high"),),
        }
    )
    run = engine.start("Write result", cfg, source)
    calls = details(engine.journal, run)["invocations"]
    model_calls = [c for c in calls if c["model"] is not None]
    assert [(c["model"], c["effort"]) for c in model_calls] == [
        (p.model, p.effort) for p in model.policies
    ]
    assert any(c["model"] == "reviewer" and c["effort"] == "max" for c in model_calls)
    assert all(c["usage"] == USAGE.model_dump() for c in model_calls)
    if repair:
        assert any(c["model"] == "stronger" for c in model_calls)
