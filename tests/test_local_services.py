"""Operator capability, durable identity and native projection; no real sockets."""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from pydantic import ValidationError

from ai_employee.container import ContainerModel
from ai_employee.history import Journal
from ai_employee.isolated_worker import IsolatedWorkerProfile
from ai_employee.models import Authority, Clarification, RunConfig
from ai_employee.native import codex_permissions
from ai_employee.product_capabilities import execution_environment

from .test_autonomous_runtime import config
from .test_stage_contracts import clarification, stream


@pytest.mark.parametrize("value", [0, -1, 15, 4097, True, "128", 128.5])
def test_service_storage_requires_explicit_bounded_integer(value):
    with pytest.raises(ValidationError):
        IsolatedWorkerProfile(image="sha256:" + "a" * 64, local_service_storage_mb=value)


def test_historical_disabled_identity_and_enabled_replay(tmp_path):
    old = config().model_dump(mode="json")
    old["isolation"] = IsolatedWorkerProfile(image="sha256:" + "a" * 64).model_dump()
    original = RunConfig.model_validate(old).canonical()
    assert "local_service_storage_mb" not in original
    restored = RunConfig.model_validate_json(original)
    assert restored.digest == hashlib.sha256(original.encode()).hexdigest()
    enabled = json.loads(original)
    enabled["isolation"]["local_service_storage_mb"] = 128
    cfg = RunConfig.model_validate(enabled)
    assert cfg.digest != restored.digest
    journal = Journal(tmp_path / "history.db")
    run = journal.create("local fixture", cfg)
    saved = Journal(journal.path).config(run)
    assert saved.digest == cfg.digest
    assert saved.isolation.local_service_storage_mb == 128


@pytest.mark.parametrize("storage", [None, 128])
@pytest.mark.parametrize("hosts", [(), ("example.com",)])
def test_service_environment_and_permissions_do_not_expand_external_authority(storage, hosts):
    args = codex_permissions(
        Path("/work"), Authority(network_hosts=hosts), local_service_storage_mb=storage
    )
    policy = " ".join(args)
    enabled = storage is not None
    assert ('"/fleet-runtime"="write"' in policy) == enabled
    assert ("network.enabled=true" in policy) == (enabled or bool(hosts))
    if enabled or hosts:
        domains = next(a for a in args if a.startswith("permissions.fleet-worker.network.domains="))
        assert domains.endswith('{"example.com"="allow"}' if hosts else "{}")
        assert "features.network_proxy=true" in policy
    assert execution_environment(storage)["local_services"]["enabled"] == enabled
    assert '":root"' not in policy
    assert "danger-full-access" not in policy


def test_local_readiness_failure_stops_before_work():
    candidate = Mock()
    candidate.profile.local_service_storage_mb = 128
    candidate.run_guarded.return_value = (79, b"", b"")
    with pytest.raises(ValueError, match="LOCAL_SERVICE_SANDBOX_UNAVAILABLE"):
        ContainerModel._native_probe(candidate)
    script = candidate.run_guarded.call_args.args[0][-1]
    assert "/fleet-runtime" in script
    assert "socket.create_connection" in script
    compile(script, "local-service-readiness", "exec")


def test_worker_context_and_independent_check_use_the_operator_setting(tmp_path):
    profile = IsolatedWorkerProfile(
        image="sha256:" + "a" * 64, auth_file="/fixture-auth", local_service_storage_mb=128
    )
    model = ContainerModel(profile)
    candidate = MagicMock()
    candidate.profile = profile
    candidate.deadline = None
    candidate.proxy = None
    candidate.run_guarded.return_value = (
        0,
        stream(clarification().model_dump(mode="json")).encode(),
        b"",
    )
    with (
        patch.object(model, "_candidate") as factory,
        patch.object(model, "_native_probe"),
        patch.object(model, "_copy_workspace"),
    ):
        factory.return_value.__enter__.return_value = candidate
        model.generate(
            config().clarification, "{}", Clarification, tmp_path, Authority(), None, lambda: False
        )
        call = candidate.run_guarded.call_args
        body = json.loads(call.kwargs["stdin"])
        assert body["execution_environment"] == execution_environment(128)
        assert '"/fleet-runtime"="write"' in " ".join(call.args[0])
        candidate.run_guarded.return_value = (0, b"", b"")
        assert model.check(("fixture-tests",), tmp_path, None, lambda: False)[0]
        policy = " ".join(candidate.run_guarded.call_args.args[0])
        assert '"/fleet-runtime"="write"' in policy
        assert "network.domains={}" in policy


def test_model_authority_cannot_enable_local_service_environment():
    with pytest.raises(ValidationError):
        Authority.model_validate({"local_service_storage_mb": 128})
