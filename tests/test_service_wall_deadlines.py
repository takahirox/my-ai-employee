import io

import pytest

from ai_employee.domain.v2 import DecisionOutcome, DownloadRequest, PolicyDecision
from ai_employee.run_budget import WallTimeExceeded, wall_budget_scope
from ai_employee.services_v2 import AtomicArtifactStore, RestrictedDownloadClient, TransportResponse
from ai_employee.storage import SQLiteStore
from tests import test_browser_services as browser


@pytest.mark.parametrize("late", ["none", "transport", "eof", "cancel-eof"])
def test_download_uses_remaining_time_and_rejects_late_final_read(tmp_path, late):
    clock = [0.0]
    cancellation = browser.Cancellation()
    limits = []

    class Body(io.BytesIO):
        def read(self, size=-1):
            if late == "eof":
                clock[0] += 2.0
            elif late == "cancel-eof":
                cancellation.value = True
            return super().read(size)

    body = Body(b"ok" if late == "none" else b"")

    def transport(url, peer, connect_timeout, read_timeout):
        limits.extend((connect_timeout, read_timeout))
        if late == "transport":
            clock[0] += 2.0
        return TransportResponse(200, {}, body, peer)

    request = DownloadRequest(
        id="download",
        run_id="run-1",
        created_at=browser.NOW,
        url="https://example.test/file",
        maximum_bytes=100,
        timeout_seconds=20.0,
        destination_kind="source",
        purpose="bounded offline deadline test",
    )
    decision = PolicyDecision(
        id="allow",
        run_id=request.run_id,
        created_at=browser.NOW,
        request_digest=request.content_digest,
        effective_policy_digest=browser.ZERO,
        outcome=DecisionOutcome.ALLOW,
        reason_code="fixture",
    )
    artifacts = AtomicArtifactStore(tmp_path / "downloads")
    client = RestrictedDownloadClient(
        artifacts,
        enabled=True,
        allowed_domains=("example.test",),
        resolver=lambda *_: ("93.184.216.34",),
        transport=transport,
    )
    with (
        SQLiteStore(tmp_path / "download.db") as store,
        wall_budget_scope(
            store,
            "run-1",
            10.0,
            clock=lambda: clock[0],
        ),
    ):
        clock[0] = 9.0
        if late in {"transport", "eof"}:
            with pytest.raises(WallTimeExceeded):
                client.fetch(request, decision, cancellation)
        else:
            result = client.fetch(request, decision, cancellation)
            assert result.status == ("succeeded" if late == "none" else "cancelled")
            assert result.request_digest == request.content_digest
    assert limits and all(0 < value <= 1.0 for value in limits)
    assert body.closed
    assert request.timeout_seconds == 20.0
    if late != "none":
        assert not list(artifacts.metadata_root.iterdir())


@pytest.mark.parametrize("late", ["none", "open", "capture", "cancel-capture"])
def test_browser_caps_actions_and_rejects_late_capture_with_cleanup(tmp_path, late):
    clock = [0.0]
    cancellation = browser.Cancellation()
    (tmp_path / "workspace").mkdir()

    class Engine(browser.FakeEngine):
        def open(self, handler):
            super().open(handler)
            if late == "open":
                clock[0] += 2.0

        def screenshot(self, seconds):
            assert seconds <= 1.0
            if late == "capture":
                clock[0] += 2.0
            if late == "cancel-capture":
                cancellation.value = True
            return super().screenshot(seconds)

    engine = Engine()
    service, artifacts = browser.services(tmp_path, engine, cancellation)
    scenario = browser.scenario()
    request = browser.request(scenario)
    with (
        SQLiteStore(tmp_path / "browser.db") as store,
        wall_budget_scope(
            store,
            "run-1",
            10.0,
            clock=lambda: clock[0],
        ),
    ):
        clock[0] = 9.0
        if late == "open":
            with pytest.raises(WallTimeExceeded):
                service.open_browser(scenario, request)
        else:
            session = service.open_browser(scenario, request)
            if late == "capture":
                with pytest.raises(WallTimeExceeded):
                    service.observe_browser(session, scenario, request)
            else:
                result = service.observe_browser(session, scenario, request)
                assert result.status == ("cancelled" if late == "cancel-capture" else "succeeded")
                assert result.request_digest == request.content_digest
                service.teardown_browser(session)
    assert engine.closed == ["page", "context", "browser", "engine"]
    assert all(action[-1] <= 1.0 for action in engine.actions)
    if late != "none":
        assert not list(artifacts.metadata_root.iterdir())
