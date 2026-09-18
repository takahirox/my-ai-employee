"""Removed configuration and strict supervisor completion boundaries."""

import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from ai_employee.isolated_worker import DockerCandidate, IsolatedWorkerProfile


def test_cumulative_configuration_is_removed_without_compatibility():
    profile = IsolatedWorkerProfile(image="sha256:" + "a" * 64)
    assert profile.pids_limit == 128
    assert "native_process_limit" not in profile.model_dump()
    assert "native_process_limit" not in profile.model_json_schema()["properties"]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        IsolatedWorkerProfile.model_validate({**profile.model_dump(), "native_process_limit": 512})


@pytest.mark.parametrize(
    "report",
    [
        {"root_exit": 0, "cleanup": "unknown"},
        {"root_exit": True, "cleanup": "confirmed"},
        {"cleanup": "confirmed"},
        [],
    ],
)
def test_invalid_completion_never_authorizes_snapshot(tmp_path, monkeypatch, report):
    candidate = DockerCandidate(
        IsolatedWorkerProfile(image="sha256:" + "a" * 64),
        tmp_path,
        seconds=30,
        cancellation=Mock(cancelled=lambda: False),
    )
    candidate.native_completion = {"root_exit": 0, "cleanup": "confirmed"}
    monkeypatch.setattr(candidate, "_docker", lambda *a, **k: json.dumps(report).encode())
    monkeypatch.setattr(candidate, "run", lambda *a, **k: (0, b"", b""))
    with pytest.raises(RuntimeError, match="ISOLATION_PROCESS_GUARD_FAILED"):
        candidate.run_guarded(("fixture",))
    assert candidate.native_completion == {}
