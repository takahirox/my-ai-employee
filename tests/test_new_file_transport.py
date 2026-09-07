import json
import subprocess

import pytest

from ai_employee.services_v2._common import now
from ai_employee.worker_adapters import _validate_edit_intent_diffs, _validate_worker_envelope


def envelope(files, **overrides):
    identity = {
        "id": "edit",
        "run_id": "run",
        "created_at": now().isoformat(),
        "schema_version": "2",
    }
    return {
        "schema_version": "2",
        "proposals": [
            {
                **identity,
                "worker_id": "codex_cli",
                "kind": "edit_intent",
                "reason": "fixture",
                "expected_artifact_kinds": ["workspace_patch"],
                "payload": {
                    **identity,
                    "paths": [f["path"] for f in files],
                    "summary": "new files",
                    "files": files,
                    **overrides,
                },
            }
        ],
    }


def compile_files(files, **overrides):
    result = _validate_worker_envelope(json.dumps(envelope(files, **overrides)))
    _validate_edit_intent_diffs(result)
    return result.proposals[0].payload.unified_diff


@pytest.mark.parametrize("ending", ["", "\n"])
def test_multifile_literal_contents_survive_compile_and_apply(tmp_path, ending):
    files = [
        {"path": "src/program.py", "content": "print('日本語')" + ending},
        {"path": "output/manifest.json", "content": '{"script":"src/program.py"}\n'},
        {"path": "src/literal.txt", "content": "diff --git a/foo b/foo\n+line\n@@ x\n"},
    ]
    patch = compile_files(files)
    subprocess.run(
        ["git", "apply", "-"], cwd=tmp_path, input=patch, text=True, capture_output=True, check=True
    )
    for file in files:
        assert (tmp_path / file["path"]).read_bytes() == file["content"].encode()
    # The exact same transport must not overwrite an existing destination.
    assert (
        subprocess.run(
            ["git", "apply", "--check", "-"],
            cwd=tmp_path,
            input=patch,
            text=True,
            capture_output=True,
        ).returncode
        != 0
    )


@pytest.mark.parametrize(
    "path", ["../escape", "/absolute", "src/../input/file", "src/a\nfile", "src\\file"]
)
def test_new_file_path_traversal_is_rejected(path):
    with pytest.raises(ValueError):
        compile_files([{"path": path, "content": "text\n"}])


def test_ambiguous_duplicate_and_oversized_inputs_are_rejected():
    files = [{"path": "src/file", "content": "text"}]
    for overrides in ({"unified_diff": "diff"}, {"paths": ["other"]}):
        with pytest.raises(ValueError):
            compile_files(files, **overrides)
    for invalid in (
        files * 2,
        [{"path": "src/file", "content": "x" * 1_000_001}],
        [{"path": "src/file", "content": "\x00"}],
        [{"path": "src/file", "content": ""}],
    ):
        with pytest.raises(ValueError):
            compile_files(invalid)
