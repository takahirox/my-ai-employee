from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_employee import benchmark_adapter as adapter
from ai_employee.domain.harness import ProjectHarnessV2
from ai_employee.isolated_worker import DockerCandidate, IsolatedWorkerProfile


def test_harness_has_only_public_structural_checks():
    value = adapter.make_harness(180.0)
    harness = ProjectHarnessV2.model_validate_json(json.dumps(value))
    assert harness.worker.isolated_workspace_tools is True
    assert harness.budgets.processes == 600 and harness.budgets.worker_turns == 1
    assert harness.paths.writable == ("src/**", "output/**")
    assert "public-checks/smoke.py" in harness.commands["smoke"].argv[-1]
    assert "/tests" not in json.dumps(value)


def test_ledger_is_explicit_absolute_and_not_exported(tmp_path):
    with pytest.raises(ValueError):
        IsolatedWorkerProfile(image="sha256:" + "0" * 64, resource_ledger="relative.json")
    ledger = tmp_path / "resources.jsonl"
    profile = IsolatedWorkerProfile(image="sha256:" + "0" * 64, resource_ledger=str(ledger))
    candidate = DockerCandidate(
        profile, tmp_path, seconds=2, cancellation=SimpleNamespace(cancelled=lambda: False)
    )
    candidate._record_resource("container", candidate.name)
    assert json.loads(ledger.read_text()) == {
        "kind": "container",
        "name": candidate.name,
        "state": "intent",
    }
    assert ledger.stat().st_mode & 0o777 == 0o600


def test_interrupted_create_never_reports_cleanup_confirmed(tmp_path, monkeypatch):
    name = "fleet-candidate-" + "b" * 32
    (tmp_path / "resources.jsonl").write_text(
        json.dumps({"kind": "container", "name": name, "state": "intent"})
    )
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1, stderr=f"No such container: {name}".encode()
        ),
    )
    with pytest.raises(RuntimeError, match="creation was interrupted"):
        adapter.cleanup(tmp_path)


def test_cleanup_only_exact_owned_resources_and_in_correct_order(tmp_path, monkeypatch):
    prefix = "fleet-candidate-" + "a" * 32
    resources = [
        {"kind": "network", "name": prefix + "-network"},
        {"kind": "container", "name": prefix + "-proxy"},
        {"kind": "container", "name": prefix},
    ]
    (tmp_path / "resources.jsonl").write_text("\n".join(map(json.dumps, resources)))
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(adapter.subprocess, "run", execute)
    adapter.cleanup(tmp_path)
    assert len(calls) == 3 and calls[-1] == ["docker", "network", "rm", prefix + "-network"]
    assert all("prune" not in c and "-v" not in c for c in calls)


@pytest.mark.parametrize("name", ["other-workload", "fleet-candidate-abc", "--all", "/"])
def test_cleanup_rejects_unowned_names_before_any_deletion(tmp_path, monkeypatch, name):
    (tmp_path / "resources.jsonl").write_text(json.dumps({"kind": "container", "name": name}))
    monkeypatch.setattr(adapter.subprocess, "run", lambda *a, **kw: pytest.fail("must not delete"))
    with pytest.raises(ValueError, match="owned resource"):
        adapter.cleanup(tmp_path)


def test_adapter_protocol_cleanup_without_model_or_credentials(tmp_path):
    request, response = tmp_path / "request.json", tmp_path / "response.json"
    request.write_text(
        json.dumps(
            {"protocol": adapter.PROTOCOL, "operation": "cleanup", "control_dir": str(tmp_path)}
        )
    )
    assert adapter.main(["--request", str(request), "--response", str(response)]) == 0
    assert json.loads(response.read_text())["outcome"] == "cleaned"


@pytest.mark.parametrize("missing_resource", [True, False])
def test_cleanup_distinguishes_missing_network_from_missing_daemon(
    tmp_path, monkeypatch, missing_resource
):
    name = "fleet-candidate-" + "a" * 32 + "-network"
    (tmp_path / "resources.jsonl").write_text(json.dumps({"kind": "network", "name": name}))
    error = (
        f"Error response from daemon: network {name} not found"
        if missing_resource
        else "docker.sock: No such file or directory"
    )
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stderr=error.encode()),
    )
    if missing_resource:
        adapter.cleanup(tmp_path)
    else:
        with pytest.raises(RuntimeError, match="not confirmed"):
            adapter.cleanup(tmp_path)


def test_adapter_does_not_run_outside_control_directory(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        adapter.checked_directory(str(tmp_path.parent), tmp_path)


def test_regular_files_reject_symlinks(tmp_path):
    (tmp_path / "outside").symlink_to(Path("/"))
    with pytest.raises(ValueError, match="symlink"):
        adapter.regular_files(tmp_path)


def public_request(tmp_path):
    workspace = tmp_path / "workspace"
    checks = tmp_path / "public-checks"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/example.py").write_text("value = 0\n")
    (workspace / "input").mkdir()
    (workspace / "input/public.txt").write_text("public fixture")
    checks.mkdir()
    (checks / "smoke.py").write_text("# public structural check\n")
    (checks / "execution.py").write_text("# public execution protocol\n")
    return {
        "workspace": str(workspace),
        "public_checks": str(checks),
        "seconds": 180.0,
        "writable_roots": ["src", "output"],
        "model": "fixed-test-model",
        "effort": "low",
        "instruction": "public task",
        "settings": {
            "isolated_worker": {
                "image": "sha256:" + "0" * 64,
                "auth_file": str(tmp_path / "dummy-auth.json"),
            }
        },
    }


@pytest.mark.parametrize("successful", [True, False])
def test_product_connection_uses_private_state_and_only_returns_candidate(
    tmp_path, monkeypatch, successful
):
    request = public_request(tmp_path)

    def work(args):
        from ai_employee.config import load_operator_config

        assert Path(args.db).is_relative_to(tmp_path / "state")
        assert args.routing_mode == "fixed" and args.strategy == "benchmark"
        assert args.goal == "public task"
        configured = load_operator_config(args.operator_config)
        assert configured.isolated_worker is not None
        assert configured.routing.strategies[0].model == "fixed-test-model"
        with adapter.SQLiteStore(Path(args.db)):
            pass
        print(
            json.dumps(
                {"run_id": "fixture", "status": "ready_to_promote" if successful else "failed"}
            )
        )
        return 0 if successful else 1

    def diff(store, args):
        assert args.run_id == "fixture" and args.stat is False
        print(
            "diff --git a/src/example.py b/src/example.py\n"
            "--- a/src/example.py\n+++ b/src/example.py\n@@ -1 +1 @@\n-value = 0\n+value = 1"
        )
        return 0

    monkeypatch.setattr(adapter.cli, "_work", work)
    monkeypatch.setattr(adapter.cli, "_diff", diff)
    result = adapter.run(request, tmp_path)
    assert result["outcome"] == "completed"  # not a score, including the failed attempt
    assert result["usage"] == {}
    assert (tmp_path / "workspace/src/example.py").read_text() == f"value = {int(successful)}\n"
    assert not (tmp_path / "workspace/.fleet").exists()
    assert (tmp_path / "workspace/input/public.txt").read_text() == "public fixture"
