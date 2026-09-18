"""Configured snapshot admission, transport, publication, and historical replay."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from ai_employee.candidates import Candidates
from ai_employee.cli import main
from ai_employee.container import ContainerModel
from ai_employee.history import Journal
from ai_employee.isolated_worker import DockerCandidate, IsolatedWorkerProfile
from ai_employee.models import Candidate, RunConfig
from ai_employee.snapshot import (
    INIT_SNAPSHOT_BYTES,
    LEGACY_SNAPSHOT_BYTES,
    archive_limit,
    pack_workspace,
    unpack_workspace,
)

from .test_autonomous_runtime import OfflineModel, config


def init_args(tmp_path, *extra):
    return [
        "init",
        "--model",
        "fixture",
        "--image",
        "sha256:" + "a" * 64,
        "--auth-file",
        str(tmp_path / "unused-auth"),
        "--output",
        str(tmp_path / "config.json"),
        *extra,
    ]


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ([], (512 * 1024**2, 1024)),
        (["--snapshot-max-bytes", "1048576", "--workspace-mb", "16"], (1048576, 16)),
    ],
)
def test_init_explicit_defaults_and_operator_overrides(tmp_path, overrides, expected):
    assert main(init_args(tmp_path, *overrides)) == 0
    payload = json.loads((tmp_path / "config.json").read_text())
    assert (payload["snapshot_max_bytes"], payload["isolation"]["workspace_mb"]) == expected
    assert RunConfig.model_validate(payload).snapshot_max_bytes == expected[0]


@pytest.mark.parametrize("value", [0, -1, True, None, 1.5, "512", float("inf")])
def test_snapshot_allowance_requires_positive_integer(value):
    with pytest.raises(ValidationError):
        RunConfig.model_validate({**config().model_dump(), "snapshot_max_bytes": value})


def test_init_rejects_invalid_allowance_without_writing(tmp_path):
    assert main(init_args(tmp_path, "--snapshot-max-bytes", "0")) == 2
    assert not (tmp_path / "config.json").exists()


def test_old_config_identity_and_saved_run_remain_readable(tmp_path):
    old = config().model_dump(
        mode="json", exclude={"snapshot_max_bytes", "command_capture", "direct_execution"}
    )
    profile = IsolatedWorkerProfile(image="sha256:" + "a" * 64)
    old["isolation"] = profile.model_dump(mode="json")
    historical_text = json.dumps(old, sort_keys=True, separators=(",", ":"))
    restored = RunConfig.model_validate_json(historical_text)
    assert restored.snapshot_max_bytes == LEGACY_SNAPSHOT_BYTES
    assert restored.isolation.workspace_mb == 256
    assert restored.canonical() == historical_text
    assert restored.digest == hashlib.sha256(historical_text.encode()).hexdigest()
    omitted = dict(old)
    omitted["isolation"] = dict(old["isolation"])
    omitted["isolation"].pop("workspace_mb")
    assert RunConfig.model_validate(omitted).isolation.workspace_mb == 256
    journal = Journal(tmp_path / "state/history.db")
    run = journal.create("old run", restored)
    with journal.connect() as db:
        assert (
            db.execute("SELECT config FROM runs WHERE id=?", (run,)).fetchone()[0]
            == historical_text
        )
    assert Journal(journal.path).config(run).digest == restored.digest


def test_reported_size_through_cli_resume_result_and_publication(tmp_path, capsys):
    source = tmp_path / "source"
    source.mkdir()
    payload = source / "asset.bin"
    with payload.open("wb") as stream:
        stream.truncate(201_830_558)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "asset.bin"], check=True)
    cfg = config().model_copy(update={"snapshot_max_bytes": INIT_SNAPSHOT_BYTES})
    path = tmp_path / "config.json"
    path.write_text(cfg.model_dump_json())
    state = tmp_path / "state"
    model = OfflineModel()
    limits = []

    def backend(profile, *, snapshot_max_bytes):
        limits.append(snapshot_max_bytes)
        return model

    with patch("ai_employee.cli.ContainerModel", side_effect=backend):
        assert (
            main(
                [
                    "--state",
                    str(state),
                    "submit",
                    "Write result",
                    "--root",
                    str(source),
                    "--config",
                    str(path),
                ]
            )
            == 0
        )
        run = json.loads(capsys.readouterr().out.splitlines()[0])["run_id"]
        assert model.calls == []
        # Resume must use the saved Run, not a modified operator configuration.
        path.write_text(config().model_dump_json())
        payload.unlink()
        assert main(["--state", str(state), "resume", run]) == 0
        assert model.workers == 1
        assert main(["--state", str(state), "result", run]) == 0
        assert (
            main(
                [
                    "--state",
                    str(state),
                    "promote",
                    run,
                    "--destination",
                    str(tmp_path / "published"),
                ]
            )
            == 0
        )
        before = list(model.calls)
        assert main(["--state", str(state), "resume", run]) == 0
        assert model.calls == before
    assert set(limits) == {INIT_SNAPSHOT_BYTES}
    assert (tmp_path / "published/asset.bin").stat().st_size == 201_830_558
    assert (tmp_path / "published/result.txt").read_text() == "correct"
    assert Journal(state / "history.db").config(run).snapshot_max_bytes == INIT_SNAPSHOT_BYTES


def test_input_overflow_reports_limit_partial_bytes_and_no_model_call(tmp_path, capsys):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"abcdef")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    cfg = tmp_path / "config.json"
    cfg.write_text(config().model_copy(update={"snapshot_max_bytes": 5}).model_dump_json())
    model = OfflineModel()
    with patch("ai_employee.cli.ContainerModel", return_value=model):
        assert (
            main(
                [
                    "--state",
                    str(tmp_path / "state"),
                    "work",
                    "Write result",
                    "--root",
                    str(source),
                    "--config",
                    str(cfg),
                ]
            )
            == 2
        )
    error = json.loads(capsys.readouterr().out)["error"]
    assert error == "INPUT_SIZE_LIMIT: limit_bytes=5, observed_bytes=6, count=partial"
    assert model.calls == []


def test_snapshot_bounds_include_duplicates_links_and_publication(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"abc")
    (source / "b").write_bytes(b"abc")
    (source / "link").symlink_to("a")
    candidates = Candidates(tmp_path / "objects", max_bytes=7)
    tree = candidates.capture(source)
    candidates.materialize(tree, tmp_path / "restored")
    assert os.readlink(tmp_path / "restored/link") == "a"
    lower = Candidates(candidates.root, max_bytes=6)
    with pytest.raises(ValueError, match="limit_bytes=6, observed_bytes=7"):
        lower.capture(source)
    with pytest.raises(ValueError, match="limit_bytes=6, observed_bytes=7"):
        lower.manifest(tree)
    with pytest.raises(ValueError, match="CANDIDATE_SIZE_LIMIT"):
        lower.materialize(tree, tmp_path / "too-small")
    candidate = Candidate(
        tree=tree, task_digest="a" * 64, attempt_id="test", authority_version=0, upstream=()
    )
    with pytest.raises(ValueError, match="CANDIDATE_SIZE_LIMIT"):
        lower.export_tree(candidate.tree, tmp_path / "not-published")
    assert not (tmp_path / "not-published").exists()


def test_return_archive_limit_and_content_limit_are_distinct(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "file").write_bytes(b"abcdef")
    data = pack_workspace(root, 6)
    unpack_workspace(data, tmp_path / "allowed", 6)
    with pytest.raises(ValueError, match="limit_bytes=5, observed_bytes=6, count=partial"):
        unpack_workspace(data, tmp_path / "denied", 5)
    assert not (tmp_path / "denied").exists()
    with pytest.raises(ValueError, match=r"CANDIDATE_TRANSPORT_SIZE_LIMIT.*count=complete"):
        unpack_workspace(b"x" * (archive_limit(1) + 1), tmp_path / "oversized", 1)
    assert not (tmp_path / "oversized").exists()
    assert archive_limit(INIT_SNAPSHOT_BYTES) > INIT_SNAPSHOT_BYTES


def test_workspace_capacity_is_separate_and_does_not_weaken_snapshot_limit(tmp_path):
    (tmp_path / "file").write_bytes(b"abcdef")
    with pytest.raises(
        ValueError, match="WORKSPACE_SIZE_LIMIT: limit_bytes=5, observed_bytes=6, count=complete"
    ):
        pack_workspace(tmp_path, 100, workspace_limit=5)
    with pytest.raises(ValueError, match="CANDIDATE_SIZE_LIMIT: limit_bytes=5"):
        pack_workspace(tmp_path, 5, workspace_limit=100)


def test_actual_container_upload_and_recovery_use_configured_allowance(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file").write_bytes(b"abcdef")
    isolated = tmp_path / "isolated"
    profile = IsolatedWorkerProfile(image="sha256:" + "a" * 64)
    model = ContainerModel(profile, snapshot_max_bytes=6)

    def docker(candidate, *args, data=None):
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": profile.image, "Os": "linux"}]).encode()
        if data is not None:
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                archive.extractall(isolated, filter="data")
        if args[:1] == ("exec",) and "pack_workspace" in args[-1]:
            program = args[-1].replace("Path('/work')", f"Path({str(isolated)!r})")
            return subprocess.check_output(
                [sys.executable, "-I", "-c", program], stderr=subprocess.PIPE
            )
        return b""

    with (
        patch("ai_employee.isolated_worker.owner_watch.start"),
        patch.object(DockerCandidate, "_docker", docker),
        patch.object(DockerCandidate, "_probe"),
        patch.object(DockerCandidate, "quiesce"),
        patch.object(DockerCandidate, "close"),
        model._candidate(workspace, None, lambda: False, models=False) as candidate,
    ):
        assert (isolated / "file").read_bytes() == b"abcdef"
        model._copy_workspace(candidate, workspace)
        (isolated / "file").write_bytes(b"abcdefg")
        with pytest.raises(subprocess.CalledProcessError) as raised:
            model._copy_workspace(candidate, workspace)
        assert b"limit_bytes=6, observed_bytes=7" in raised.value.stderr
        assert (workspace / "file").read_bytes() == b"abcdef"


def test_changed_saved_allowance_cannot_bypass_run_identity(tmp_path):
    cfg = config().model_copy(update={"snapshot_max_bytes": 100})
    journal = Journal(tmp_path / "history.db")
    run = journal.create("test", cfg)
    with journal.connect() as db:
        db.execute(
            "UPDATE runs SET config=? WHERE id=?",
            (cfg.model_copy(update={"snapshot_max_bytes": 101}).canonical(), run),
        )
    with pytest.raises(ValueError, match="RUN_CONFIG_CHANGED"):
        Journal(journal.path).config(run)


def test_large_finite_allowance_does_not_become_a_read_allocation(tmp_path):
    cfg = RunConfig.model_validate({**config().model_dump(), "snapshot_max_bytes": 2**64})
    root = tmp_path / "source"
    root.mkdir()
    (root / "file").write_bytes(b"small input")
    candidates = Candidates(tmp_path / "objects", max_bytes=cfg.snapshot_max_bytes)
    tree = candidates.capture(root)
    candidates.materialize(tree, tmp_path / "restored")
    data = pack_workspace(root, cfg.snapshot_max_bytes)
    unpack_workspace(data, tmp_path / "returned", cfg.snapshot_max_bytes)
    assert (tmp_path / "restored/file").read_bytes() == b"small input"
    assert (tmp_path / "returned/file").read_bytes() == b"small input"


def test_slow_docker_consumer_receives_all_input_across_poll_timeouts(tmp_path):
    from ai_employee.container import Cancellation

    candidate = DockerCandidate(
        IsolatedWorkerProfile(image="sha256:" + "a" * 64),
        tmp_path,
        seconds=3,
        cancellation=Cancellation(lambda: False),
    )
    payload = b"archive-content" * 100_000
    real_popen = subprocess.Popen
    handles = []

    def slow_consumer(argv, **kwargs):
        assert argv[:2] == ["docker", "exec"]
        handles.append(kwargs["stdin"])
        return real_popen(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys,time,hashlib;time.sleep(.2);"
                "print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())",
            ],
            **kwargs,
        )

    with patch("ai_employee.isolated_worker.subprocess.Popen", side_effect=slow_consumer):
        assert (
            candidate._docker("exec", "fixture", data=payload).decode().strip()
            == hashlib.sha256(payload).hexdigest()
        )

    assert handles[0].closed
