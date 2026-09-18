"""Content-addressed artifact capture, isolated materialization and fresh promotion."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .models import Candidate, TaskContext
from .snapshot import PROTECTED, read_entries, validate_entries


class Candidates:
    def __init__(self, root: Path, *, max_bytes: int = 64_000_000) -> None:
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def compare_inputs(
        self, initial: str, candidate: str, paths: tuple[str, ...]
    ) -> dict[str, Any]:
        """Compare authenticated snapshots; bounded details never truncate the verdict."""
        before, after = self.manifest(initial), self.manifest(candidate)

        def selected(name: str, scope: str) -> bool:
            return scope == "." or name == scope or name.startswith(scope + "/")

        missing = [scope for scope in paths if not any(selected(p, scope) for p in before)]
        names = sorted(
            p for p in before.keys() | after.keys() if any(selected(p, s) for s in paths)
        )
        changed = [p for p in names if before.get(p) != after.get(p)]
        return {
            "matches": not missing and not changed,
            "compared_paths": len(names),
            "missing_initial_paths": missing,
            "changed_count": len(changed),
            "changes": [
                {"path": p, "before": before.get(p), "after": after.get(p)} for p in changed[:64]
            ],
            "truncated": len(changed) > 64,
        }

    def capture_source(self, source: Path) -> str:
        """A repository exposes tracked files only; untracked operator files stay private."""
        if not (source / ".git").exists():
            return self.capture(source)
        names = (
            subprocess.check_output(
                ["git", "-C", str(source), "ls-files", "--cached", "-z"], timeout=10
            )
            .decode()
            .split("\0")
        )
        selected = [
            name
            for name in names
            if name and not any(part in PROTECTED for part in PurePosixPath(name).parts)
        ]
        try:
            entries = read_entries(source.resolve(), self.max_bytes, names=selected)
        except ValueError as error:
            input_errors = {
                "CANDIDATE_SIZE_LIMIT": "INPUT_SIZE_LIMIT",
                "CANDIDATE_UNSAFE_PATH": "INPUT_UNSAFE_PATH",
                "CANDIDATE_SPECIAL_FILE": "INPUT_SPECIAL_FILE",
            }
            if str(error) in input_errors:
                raise ValueError(input_errors[str(error)]) from error
            raise
        return self._store(entries)

    def capture(self, source: Path) -> str:
        """Capture bounded files and validated relative links from a quiesced workspace."""
        source = source.resolve()
        if not source.is_dir():
            raise ValueError("SOURCE_DIRECTORY_UNAVAILABLE")
        if self.root == source or self.root.is_relative_to(source):
            raise ValueError("CANDIDATE_STORE_INSIDE_WORKSPACE")
        return self._store(read_entries(source, self.max_bytes))

    def _store(self, entries: dict[str, dict[str, Any]]) -> str:
        manifest: dict[str, dict[str, str | bool]] = {}
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            staging = Path(temporary)
            for name, entry in entries.items():
                if "target" in entry:
                    manifest[name] = {"target": entry["target"]}
                    continue
                data = entry["data"]
                digest = hashlib.sha256(data).hexdigest()
                blob = staging / digest
                if not blob.exists():
                    blob.write_bytes(data)
                    blob.chmod(0o400)
                manifest[name] = {"blob": digest, "executable": entry["executable"]}
            encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            tree = hashlib.sha256(encoded).hexdigest()
            (staging / "manifest.json").write_bytes(encoded)
            destination = self.root / tree
            try:
                staging.rename(destination)
            except OSError:
                if not destination.exists():
                    raise
                self.manifest(tree)
            return tree

    def manifest(self, tree: str) -> dict[str, dict[str, str | bool]]:
        if len(tree) != 64 or any(c not in "0123456789abcdef" for c in tree):
            raise ValueError("INVALID_CANDIDATE_DIGEST")
        encoded = (self.root / tree / "manifest.json").read_bytes()
        if hashlib.sha256(encoded).hexdigest() != tree:
            raise ValueError("CANDIDATE_MANIFEST_CHANGED")
        manifest: dict[str, dict[str, str | bool]] = json.loads(encoded)
        validate_entries(manifest)
        for entry in manifest.values():
            if "target" in entry:
                if set(entry) != {"target"}:
                    raise ValueError("INVALID_CANDIDATE_ENTRY")
                continue
            if set(entry) != {"blob", "executable"} or type(entry["executable"]) is not bool:
                raise ValueError("INVALID_CANDIDATE_ENTRY")
            blob = str(entry["blob"])
            if len(blob) != 64 or any(c not in "0123456789abcdef" for c in blob):
                raise ValueError("INVALID_BLOB_DIGEST")
            path = self.root / tree / blob
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != blob:
                raise ValueError("CANDIDATE_BYTES_CHANGED")
        return manifest

    def materialize(self, tree: str, target: Path) -> None:
        manifest = self.manifest(tree)
        target.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or any(target.iterdir()):
            raise ValueError("MATERIALIZATION_TARGET_NOT_EMPTY")
        for name, entry in sorted(manifest.items(), key=lambda item: "target" in item[1]):
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if "target" in entry:
                destination.symlink_to(str(entry["target"]))
            else:
                shutil.copyfile(self.root / tree / str(entry["blob"]), destination)
                destination.chmod(0o700 if entry["executable"] else 0o600)
        if self.capture(target) != tree:
            raise ValueError("MATERIALIZATION_CHANGED")

    def freeze(self, context: TaskContext) -> Candidate:
        return Candidate(
            tree=self.capture(Path(context.workspace)),
            task_digest=context.task.digest,
            attempt_id=context.attempt_id,
            upstream=tuple(candidate.digest for candidate in context.upstream),
            authority_version=context.authority_version,
        )

    def publish_directory(self, candidate: Candidate, destination: Path) -> None:
        """Publish to a new directory atomically; never overwrite an operator checkout."""
        if destination.exists() or destination.is_symlink():
            raise ValueError("PROMOTION_TARGET_EXISTS")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".fleet-publish-", dir=destination.parent))
        try:
            self.materialize(candidate.tree, staging)
            # mkdir claims the exact target without overwriting even an empty directory.
            destination.mkdir()
            try:
                staging.rename(destination)
            except BaseException:
                destination.rmdir()
                raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
