"""Ownership loss is independent of a productive invocation's duration."""

import io
import subprocess
import sys
from unittest.mock import patch

import pytest

from ai_employee import owner_watch
from ai_employee.container import Cancellation
from ai_employee.isolated_worker import DockerCandidate, IsolatedWorkerProfile


@pytest.mark.parametrize("done", [False, True])
def test_watch_only_reaps_exact_owned_resources_on_eof(monkeypatch, done):
    name = "fleet-candidate-" + "a" * 32
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"D" if done else b"")))
    with (
        patch.object(owner_watch.time, "monotonic", side_effect=[0, 1, 31]),
        patch.object(owner_watch.time, "sleep"),
        patch.object(owner_watch.subprocess, "run") as remove,
    ):
        remove.return_value = subprocess.CompletedProcess([], 0, stderr=b"")
        owner_watch.watch(name)
    expected = [
        ["docker", "container", "rm", "-f", name],
        ["docker", "container", "rm", "-f", name + "-proxy"],
        ["docker", "network", "rm", name + "-network"],
    ]
    assert [call.args[0] for call in remove.call_args_list] == ([] if done else expected * 2)
    assert all(call.kwargs["timeout"] == 15 for call in remove.call_args_list)


@pytest.mark.parametrize("failure", ["unavailable", "timeout", "rejected"])
def test_watch_keeps_cleanup_alive_until_docker_recovers(monkeypatch, failure):
    name = "fleet-candidate-" + "b" * 32
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    outcomes = {
        "unavailable": OSError("daemon unavailable"),
        "timeout": subprocess.TimeoutExpired("docker", 15),
        "rejected": subprocess.CompletedProcess([], 1, stderr=b"daemon unavailable"),
    }
    removed = subprocess.CompletedProcess([], 0, stderr=b"")
    with (
        patch.object(owner_watch.time, "monotonic", side_effect=[0, 31, 32]),
        patch.object(owner_watch.time, "sleep"),
        patch.object(
            owner_watch.subprocess,
            "run",
            side_effect=[outcomes[failure], removed, removed, removed, removed, removed],
        ) as remove,
    ):
        owner_watch.watch(name)
    assert remove.call_count == 6


def test_watch_finishes_when_another_owner_already_removed_resources(monkeypatch):
    name = "fleet-candidate-" + "c" * 32
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    errors = [
        f"No such container: {name}",
        f"No such container: {name}-proxy",
        f"network {name}-network not found",
    ]
    with (
        patch.object(owner_watch.time, "monotonic", side_effect=[0, 31]),
        patch.object(
            owner_watch.subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess([], 1, stderr=error.encode()) for error in errors
            ],
        ) as remove,
    ):
        owner_watch.watch(name)
    assert remove.call_count == 3


def test_no_environment_creation_without_ready_owner_watch(tmp_path):
    candidate = DockerCandidate(
        IsolatedWorkerProfile(image="sha256:" + "a" * 64),
        tmp_path,
        seconds=None,
        cancellation=Cancellation(lambda: False),
    )
    with (
        patch.object(owner_watch, "start", side_effect=RuntimeError("OWNER_WATCH_UNAVAILABLE")),
        patch.object(candidate, "_docker") as docker,
        pytest.raises(RuntimeError, match="OWNER_WATCH_UNAVAILABLE"),
        candidate,
    ):
        pytest.fail("unowned environment admitted")
    docker.assert_not_called()


def test_unlimited_candidate_keeps_control_operations_bounded(tmp_path):
    candidate = DockerCandidate(
        IsolatedWorkerProfile(image="sha256:" + "a" * 64),
        tmp_path,
        seconds=None,
        cancellation=Cancellation(lambda: False),
    )
    with (
        patch("ai_employee.isolated_worker.subprocess.Popen") as docker,
        patch("ai_employee.isolated_worker.time.monotonic", side_effect=[0, 31]),
        pytest.raises(TimeoutError, match="DOCKER_CONTROL_TIMEOUT"),
    ):
        candidate._docker("image", "inspect", candidate.profile.image)
    assert candidate.deadline is None
    docker.return_value.kill.assert_called_once()


def test_control_operation_observes_shared_cancellation_during_communication(tmp_path):
    with patch("ai_employee.isolated_worker.subprocess.Popen") as docker:
        process = docker.return_value
        process.communicate.side_effect = [subprocess.TimeoutExpired("docker", 0.05), (b"", b"")]
        with patch(
            "ai_employee.container.Cancellation.cancelled", side_effect=[False, False, True]
        ):
            candidate = DockerCandidate(
                IsolatedWorkerProfile(image="sha256:" + "a" * 64),
                tmp_path,
                seconds=None,
                cancellation=Cancellation(lambda: False),
            )
            with pytest.raises(TimeoutError, match="DOCKER_CONTROL_TIMEOUT"):
                candidate._docker("exec", "test", data=b"input")
        process.kill.assert_called_once()
        assert process.communicate.call_args_list[0].kwargs == {"input": b"input", "timeout": 0.05}
