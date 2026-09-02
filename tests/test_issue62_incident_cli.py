from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from ai_employee import cli


def _harness(mode: str = "review") -> SimpleNamespace:
    return SimpleNamespace(incident_reporting=SimpleNamespace(mode=mode))


def _run(status: str = "failed", *, failure_code: str | None = "NODE_FAILED") -> Any:
    return SimpleNamespace(
        id="exact-run",
        status=status,
        failure_code=failure_code,
        accepted_graph_revision_digest="a" * 64,
        generation=2,
        execution_attempt=3,
    )


def _closure(run: Any, **changes: object) -> SimpleNamespace:
    values = {
        "run_id": run.id,
        "graph_run_id": run.id,
        "accepted_graph_revision_digest": run.accepted_graph_revision_digest,
        "generation": run.generation,
        "execution_attempt": run.execution_attempt,
        "terminal_graph_status": "failed",
        "content_digest": "b" * 64,
    }
    values.update(changes)
    return SimpleNamespace(**values)


class _NoIoStore:
    def get(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("unexpected incident reload")

    def list_records(self, *_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise AssertionError("unexpected incident closure read")


class _TerminalStore:
    def __init__(self, run: Any, closures: tuple[object, ...], events: list[str]) -> None:
        self.run = run
        self.closures = closures
        self.events = events

    def get(self, kind: str, run_id: str, model: object) -> Any:
        assert (kind, run_id, model) == ("graph_run_v2", "exact-run", cli.GraphRunRecord)
        self.events.append("reload")
        return self.run

    def list_records(self, kind: str, model: object, *, run_id: str) -> tuple[object, ...]:
        assert (kind, model, run_id) == (
            "run_lease_closure_v2",
            cli.RunLeaseClosureRecord,
            "exact-run",
        )
        self.events.append("closure")
        return self.closures


def _receipt(index: int = 0, **extra: object) -> Any:
    values = {
        "state": "prepared",
        "internal_incident_code": "node_failed",
        "error_code": None,
        "fingerprint": f"fingerprint-{index}",
        "report_digest": f"report-{index}",
        "preview_digest": f"preview-{index}",
        "expiry": "2026-09-03T00:00:00Z",
    }
    values.update(extra)
    return SimpleNamespace(**values)


def test_off_failed_result_performs_no_incident_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise AssertionError("incident preparation must remain off")

    monkeypatch.setattr(cli, "prepare_graph_run_incidents", forbidden)
    run, records = cli._execute_graph_run_with_incident_reporting(
        _NoIoStore(),  # type: ignore[arg-type]
        "exact-run",
        _harness("off"),  # type: ignore[arg-type]
        lambda: _run(),
    )

    assert run.status == "failed"
    assert records == ()
    assert cli._graph_run_exit_code(run) == 5


@pytest.mark.parametrize(
    "status",
    ["planned", "paused", "completed", "ready_to_promote", "cancelled", "interrupted"],
)
def test_nonfailed_results_do_not_invoke_incident_preparation(
    status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "prepare_graph_run_incidents",
        lambda *_args, **_kwargs: pytest.fail("unexpected incident preparation"),
    )

    run, records = cli._execute_graph_run_with_incident_reporting(
        _NoIoStore(),  # type: ignore[arg-type]
        "exact-run",
        _harness(),  # type: ignore[arg-type]
        lambda: _run(status, failure_code=None),
    )

    assert run.status == status
    assert records == ()
    assert cli._graph_run_cli_result(run, records)["incident_reporting"] == []


def test_returned_failed_result_prepares_once_after_service_and_keeps_exit_five(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    store = _NoIoStore()
    harness = _harness()
    receipt = _receipt()

    def execute() -> Any:
        events.append("service-result")
        return _run()

    def prepare(*args: object, **kwargs: object) -> tuple[object, ...]:
        assert events == ["service-result"]
        assert args[0] is store
        assert args[2] is harness
        assert kwargs == {
            "public_commit": cli._PUBLIC_BUILD_COMMIT_UNAVAILABLE,
            "clock": cli.now,
        }
        events.append("prepare")
        return (receipt,)

    monkeypatch.setattr(cli, "prepare_graph_run_incidents", prepare)
    run, records = cli._execute_graph_run_with_incident_reporting(
        store,  # type: ignore[arg-type]
        "exact-run",
        harness,  # type: ignore[arg-type]
        execute,
    )

    assert events == ["service-result", "prepare"]
    assert records == (receipt,)
    assert cli._graph_run_exit_code(run) == 5


@pytest.mark.parametrize(
    "error",
    [
        cli.PlanReviewGateError("PLAN_REVIEW_FAILED", "rejected"),
        cli.GraphValidationError(()),
    ],
)
def test_review_and_validation_errors_never_reach_incident_io(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "prepare_graph_run_incidents",
        lambda *_args, **_kwargs: pytest.fail("unexpected incident preparation"),
    )

    def execute() -> Any:
        raise error

    with pytest.raises(type(error)) as caught:
        cli._execute_graph_run_with_incident_reporting(
            _NoIoStore(),  # type: ignore[arg-type]
            "exact-run",
            _harness(),  # type: ignore[arg-type]
            execute,
        )
    assert caught.value is error


def test_unexpected_exception_prepares_only_after_exact_durable_failed_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    terminal = _run()
    store = _TerminalStore(terminal, (_closure(terminal),), events)
    original = RuntimeError("service canary")

    def execute() -> Any:
        events.append("service-exception")
        raise original

    def prepare(*args: object, **kwargs: object) -> tuple[object, ...]:
        assert args[:3] == (store, terminal, harness)
        assert kwargs == {
            "public_commit": cli._PUBLIC_BUILD_COMMIT_UNAVAILABLE,
            "clock": cli.now,
        }
        events.append("prepare")
        return ()

    harness = _harness()
    monkeypatch.setattr(cli, "prepare_graph_run_incidents", prepare)

    with pytest.raises(RuntimeError) as caught:
        cli._execute_graph_run_with_incident_reporting(
            store,  # type: ignore[arg-type]
            "exact-run",
            harness,  # type: ignore[arg-type]
            execute,
        )

    assert caught.value is original
    assert events == ["service-exception", "reload", "closure", "prepare"]


@pytest.mark.parametrize(
    "terminal,closures",
    [
        (_run("completed", failure_code=None), ()),
        (_run(), ()),
        (_run(), (_closure(_run(), graph_run_id="other-run"),)),
        (_run(), (_closure(_run(), content_digest=None),)),
    ],
)
def test_unexpected_exception_requires_exact_authoritative_failed_terminalization(
    terminal: Any,
    closures: tuple[object, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RuntimeError("original")
    store = _TerminalStore(terminal, closures, [])
    monkeypatch.setattr(
        cli,
        "prepare_graph_run_incidents",
        lambda *_args, **_kwargs: pytest.fail("unexpected incident preparation"),
    )

    def execute() -> Any:
        raise original

    with pytest.raises(RuntimeError) as caught:
        cli._execute_graph_run_with_incident_reporting(
            store,  # type: ignore[arg-type]
            "exact-run",
            _harness(),  # type: ignore[arg-type]
            execute,
        )
    assert caught.value is original


def test_pipeline_failure_never_masks_service_exception_or_changes_terminal_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    terminal = _run(failure_code="AUTHORITATIVE_FAILURE")
    store = _TerminalStore(terminal, (_closure(terminal),), events)
    original = RuntimeError("original service failure")

    def execute() -> Any:
        raise original

    def failing_pipeline(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise ValueError("private pipeline canary")

    monkeypatch.setattr(cli, "prepare_graph_run_incidents", failing_pipeline)
    with pytest.raises(RuntimeError) as caught:
        cli._execute_graph_run_with_incident_reporting(
            store,  # type: ignore[arg-type]
            "exact-run",
            _harness(),  # type: ignore[arg-type]
            execute,
        )

    assert caught.value is original
    assert terminal.status == "failed"
    assert terminal.failure_code == "AUTHORITATIVE_FAILURE"


def test_normal_pipeline_failure_keeps_failed_exit_and_empty_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = _run(failure_code="AUTHORITATIVE_FAILURE")
    monkeypatch.setattr(
        cli,
        "prepare_graph_run_incidents",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("pipeline failed")),
    )

    run, records = cli._execute_graph_run_with_incident_reporting(
        _NoIoStore(),  # type: ignore[arg-type]
        "exact-run",
        _harness(),  # type: ignore[arg-type]
        lambda: terminal,
    )

    assert records == ()
    assert run.status == "failed"
    assert run.failure_code == "AUTHORITATIVE_FAILURE"
    assert cli._graph_run_exit_code(run) == 5


def test_projection_is_bounded_and_ignores_every_private_canary() -> None:
    records = [
        _receipt(
            index,
            report_body=object(),
            title=object(),
            labels=object(),
            marker=object(),
            repository=object(),
            path=object(),
            run_id=object(),
            node_id=object(),
            database_id=object(),
            terminal_closure_digest=object(),
            harness_digest=object(),
            exception_message=object(),
            environment_name=object(),
            repository_key=object(),
        )
        for index in range(cli._INCIDENT_REPORTING_RESULT_LIMIT + 5)
    ]

    projected = cli._incident_reporting_projection(records)  # type: ignore[arg-type]

    assert len(projected) == cli._INCIDENT_REPORTING_RESULT_LIMIT
    assert all(
        set(item)
        == {
            "state",
            "internal_incident_code",
            "error_code",
            "fingerprint",
            "report_digest",
            "preview_digest",
            "expiry",
        }
        for item in projected
    )
    assert "canary" not in json.dumps(projected)


def test_cli_boundary_cannot_reach_publish_or_transport_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class IncidentBoundary:
        def __call__(self, *_args: object, **_kwargs: object) -> tuple[object, ...]:
            calls.append("prepare")
            return ()

        def publish(self) -> None:
            calls.append("publish")

        def request(self) -> None:
            calls.append("network")

        def transport(self) -> None:
            calls.append("transport")

    monkeypatch.setattr(cli, "prepare_graph_run_incidents", IncidentBoundary())
    cli._execute_graph_run_with_incident_reporting(
        _NoIoStore(),  # type: ignore[arg-type]
        "exact-run",
        _harness(),  # type: ignore[arg-type]
        lambda: _run(),
    )

    assert calls == ["prepare"]
