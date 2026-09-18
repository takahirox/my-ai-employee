"""Safe links across the actual CLI, snapshots, and both workspace transports."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_employee.candidates import Candidates
from ai_employee.cli import main
from ai_employee.container import ContainerModel
from ai_employee.history import Journal
from ai_employee.snapshot import pack_workspace, unpack_workspace

from .test_autonomous_runtime import OfflineModel, config

LINKS = {".windsurf/rules/chatwoot.md": "../../AGENTS.md", "CLAUDE.md": "AGENTS.md"}


def fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "AGENTS.md").write_text("tracked instructions")
    for name, target in LINKS.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
    return root


def assert_links(root):
    for name, target in LINKS.items():
        assert (root / name).is_symlink()
        assert os.readlink(root / name) == target
        assert (root / name).read_text() == "tracked instructions"


def git_track(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)


def test_cli_links_survive_execution_publication_and_completed_replay(tmp_path, capsys):
    source = fixture(tmp_path / "source")
    git_track(source)
    configuration = tmp_path / "run.json"
    configuration.write_text(config().model_dump_json())
    state = tmp_path / "state"
    model = OfflineModel()
    with patch("ai_employee.cli.ContainerModel", return_value=model):
        assert (
            main(
                [
                    "--state",
                    str(state),
                    "work",
                    "Write result",
                    "--config",
                    str(configuration),
                    "--root",
                    str(source),
                ]
            )
            == 0
        )
        run = json.loads(capsys.readouterr().out.splitlines()[0])["run_id"]
        assert model.workers == 1
        before = list(model.calls)
        assert main(["--state", str(state), "resume", run]) == 0
        assert model.calls == before
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
    assert_links(tmp_path / "published")
    assert (tmp_path / "published/result.txt").read_text() == "correct"
    assert Journal(state / "history.db").events(run)


def test_container_roundtrip_runs_actual_export_program_and_validates_return(tmp_path):
    workspace = fixture(tmp_path / "workspace")
    fixture(workspace / ".fleet-inputs/0")
    isolated = tmp_path / "isolated"
    # Exercise the actual inbound tar filter used by DockerCandidate.
    with tarfile.open(fileobj=io.BytesIO(pack_workspace(workspace, 64000000))) as archive:
        archive.extractall(isolated, filter="data")
    assert_links(isolated)
    assert_links(isolated / ".fleet-inputs/0")
    (isolated / "new.txt").write_text("worker-created")
    (isolated / "new-link").symlink_to("new.txt")
    (isolated / ".git").mkdir()
    (isolated / ".git/secret").write_text("excluded")

    class LocalContainer:
        name = "disposable"
        quiesced = False

        def quiesce(self):
            self.quiesced = True

        def _docker(self, *args):
            assert self.quiesced
            program = args[-1].replace("Path('/work')", f"Path({str(isolated)!r})")
            return subprocess.check_output([sys.executable, "-I", "-c", program])

    ContainerModel._copy_workspace(LocalContainer(), workspace)
    assert_links(workspace)
    assert_links(workspace / ".fleet-inputs/0")
    assert (workspace / "new-link").read_text() == "worker-created"
    assert not (workspace / ".git").exists()
    candidates = Candidates(tmp_path / "objects")
    tree = candidates.capture(workspace)
    assert not any(n.startswith(".fleet-inputs") for n in candidates.manifest(tree))
    candidates.materialize(tree, tmp_path / "restored")
    assert os.readlink(tmp_path / "restored/new-link") == "new.txt"


@pytest.mark.parametrize(
    "target,reason",
    [
        ("/private/host-secret", "absolute target"),
        ("../host-secret", "escapes snapshot"),
        (".git/secret", "excluded target"),
        (".fleet-inputs/0/AGENTS.md", "excluded target"),
        ("missing", "not a selected regular file"),
        ("directory", "not a selected regular file"),
        ("other-link", "chains and cycles"),
        ("link", "chains and cycles"),
        ("other-link/../AGENTS.md", "intermediate target"),
        ("missing/../AGENTS.md", "intermediate target"),
        ("AGENTS.md/", "intermediate target"),
    ],
)
def test_reject_unsafe_links_with_path_and_without_target_leak(tmp_path, target, reason):
    source = fixture(tmp_path / "source")
    (source / "directory").mkdir()
    (source / "other-link").symlink_to("AGENTS.md")
    (source / "link").symlink_to(target)
    with pytest.raises(ValueError, match=reason) as raised:
        Candidates(tmp_path / "objects").capture(source)
    assert '"link"' in str(raised.value)
    assert "host-secret" not in str(raised.value)
    with pytest.raises(ValueError, match=reason):
        pack_workspace(source, 64000000)


def test_git_rejects_untracked_referent_but_candidate_allows_new_files(tmp_path):
    source = fixture(tmp_path / "source")
    (source / "link").symlink_to("new.txt")
    git_track(source)
    (source / "new.txt").write_text("untracked")
    candidates = Candidates(tmp_path / "objects")
    with pytest.raises(ValueError, match=r'SNAPSHOT_SYMLINK.*"link"'):
        candidates.capture_source(source)
    assert candidates.manifest(candidates.capture(source))["link"] == {"target": "new.txt"}


def test_git_does_not_follow_replaced_parent_directory(tmp_path):
    source = tmp_path / "source"
    (source / "directory").mkdir(parents=True)
    (source / "directory/file").write_text("tracked")
    git_track(source)
    (source / "directory/file").unlink()
    (source / "directory").rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("host secret")
    (source / "directory").symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe or unavailable parent"):
        Candidates(tmp_path / "objects").capture_source(source)


def test_link_target_and_type_changes_affect_identity_and_comparison(tmp_path):
    source = fixture(tmp_path / "source")
    (source / "other.md").write_text("tracked instructions")
    candidates = Candidates(tmp_path / "objects")
    initial = candidates.capture(source)
    link = source / "CLAUDE.md"
    link.unlink()
    link.symlink_to("other.md")
    changed = candidates.capture(source)
    assert changed != initial
    assert candidates.compare_inputs(initial, changed, (".",))["changed_count"] == 1
    link.unlink()
    link.write_text("tracked instructions")
    regular = candidates.capture(source)
    for before, after in [(initial, regular), (regular, changed)]:
        diff = candidates.compare_inputs(before, after, ("CLAUDE.md",))
        assert diff["changed_count"] == 1 and not diff["matches"]
    manifest = candidates.root / initial / "manifest.json"
    data = json.loads(manifest.read_text())
    data["CLAUDE.md"]["target"] = "other.md"
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="CANDIDATE_MANIFEST_CHANGED"):
        candidates.materialize(initial, tmp_path / "bad")


def test_regular_file_identity_unchanged_and_restored_links_rechecked(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"contents")
    candidates = Candidates(tmp_path / "objects")
    expected = {"file": {"blob": hashlib.sha256(b"contents").hexdigest(), "executable": False}}
    tree = candidates.capture(source)
    assert (
        tree
        == hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    fixture(source)
    tree = candidates.capture(source)
    original = Path.symlink_to

    def tamper(path, target, *args, **kwargs):
        return original(path, "file" if path.name == "CLAUDE.md" else target, *args, **kwargs)

    with (
        patch.object(Path, "symlink_to", tamper),
        pytest.raises(ValueError, match="MATERIALIZATION_CHANGED"),
    ):
        candidates.materialize(tree, tmp_path / "restored")


@pytest.mark.parametrize("kind", ["escape", "parent", "hardlink", "duplicate", "cross-input"])
def test_untrusted_return_archive_is_validated_before_writes(tmp_path, kind):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        file = tarfile.TarInfo("file")
        file.size = 1
        archive.addfile(file, io.BytesIO(b"x"))
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "file"
        if kind == "escape":
            link.linkname = "../outside"
        elif kind == "parent":
            child = tarfile.TarInfo("link/child")
            archive.addfile(child, io.BytesIO(b""))
        elif kind == "hardlink":
            link.type = tarfile.LNKTYPE
        elif kind == "duplicate":
            link.name = "file"
        elif kind == "cross-input":
            link.name = ".fleet-inputs/0/link"
            link.linkname = "../../file"
        archive.addfile(link)
    destination = tmp_path / "returned"
    with pytest.raises(ValueError):
        unpack_workspace(output.getvalue(), destination, 64000000)
    assert not destination.exists()


def test_link_target_bytes_are_bounded(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "file").write_bytes(b"x")
    (root / "link").symlink_to("file")
    with pytest.raises(ValueError, match="CANDIDATE_SIZE_LIMIT"):
        Candidates(tmp_path / "objects", max_bytes=4).capture(root)
    assert Candidates(tmp_path / "objects", max_bytes=5).capture(root)


def test_deleted_tracked_directory_retains_regular_file_input_behavior(tmp_path):
    source = fixture(tmp_path / "source")
    (source / "removed").mkdir()
    (source / "removed/file").write_text("tracked then deleted")
    git_track(source)
    (source / "removed/file").unlink()
    (source / "removed").rmdir()
    candidates = Candidates(tmp_path / "objects")
    tree = candidates.capture_source(source)
    assert set(candidates.manifest(tree)) == {"AGENTS.md", *LINKS}
    candidates.materialize(tree, tmp_path / "restored")
    assert_links(tmp_path / "restored")


@pytest.mark.parametrize(
    "replacement", [{"target": "AGENTS.md"}, {"blob": "0" * 64, "executable": False}]
)
def test_stored_entry_type_tampering_is_rejected(tmp_path, replacement):
    source = fixture(tmp_path / "source")
    candidates = Candidates(tmp_path / "objects")
    tree = candidates.capture(source)
    manifest = candidates.root / tree / "manifest.json"
    data = json.loads(manifest.read_text())
    name = "AGENTS.md" if "target" in replacement else "CLAUDE.md"
    data[name] = replacement
    manifest.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")))
    with pytest.raises(ValueError, match="CANDIDATE_MANIFEST_CHANGED"):
        candidates.manifest(tree)
