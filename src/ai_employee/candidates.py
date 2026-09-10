"""Content-addressed artifact capture, isolated materialization and fresh promotion."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .models import Candidate, TaskContext

_PROTECTED = frozenset({".git", ".codex", ".claude", ".fleet", ".agents", ".fleet-inputs"})


class Candidates:
    def __init__(self, root: Path, *, max_bytes: int = 64_000_000) -> None:
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _safe(name: str) -> bool:
        path = PurePosixPath(name)
        return (
            not path.is_absolute()
            and bool(path.parts)
            and not any(part in {"", ".", ".."} | _PROTECTED for part in path.parts)
            and str(path) == name
        )

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
        with tempfile.TemporaryDirectory(prefix="fleet-input-") as directory:
            exported = Path(directory)
            total = 0
            for name in names:
                if not name or any(part in _PROTECTED for part in PurePosixPath(name).parts):
                    continue
                if not self._safe(name):
                    raise ValueError("INPUT_UNSAFE_PATH")
                path = source / name
                if any(part.is_symlink() for part in (path, *path.parents) if part != source):
                    raise ValueError("INPUT_SYMLINK")
                if not path.exists():
                    continue
                if not path.is_file():
                    raise ValueError("INPUT_SPECIAL_FILE")
                total += path.stat().st_size
                if total > self.max_bytes:
                    raise ValueError("INPUT_SIZE_LIMIT")
                destination = exported / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
                destination.chmod(0o700 if path.stat().st_mode & 0o111 else 0o600)
            return self.capture(exported)

    def capture(self, source: Path) -> str:
        """Copy bounded regular-file bytes; no symlinks, host metadata or special files.

        The immutable identity describes the bytes actually copied, not a mutable
        source directory. Callers must stop the worker before capture; mutation
        detected during traversal is rejected rather than attested as a snapshot.
        """
        source = source.resolve()
        if not source.is_dir():
            raise ValueError("SOURCE_DIRECTORY_UNAVAILABLE")
        if self.root == source or self.root.is_relative_to(source):
            raise ValueError("CANDIDATE_STORE_INSIDE_WORKSPACE")
        manifest: dict[str, dict[str, str | bool]] = {}
        total = 0
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            staging = Path(temporary)
            for directory, dirs, files in os.walk(source, followlinks=False):
                dirs[:] = sorted(name for name in dirs if name not in _PROTECTED)
                for name in dirs:
                    if (Path(directory) / name).is_symlink():
                        raise ValueError("CANDIDATE_SYMLINK")
                for name in sorted(files):
                    path = Path(directory) / name
                    relative = path.relative_to(source).as_posix()
                    if name in _PROTECTED:
                        continue
                    if not self._safe(relative):
                        raise ValueError("CANDIDATE_UNSAFE_PATH")
                    before = path.lstat()
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError("CANDIDATE_SPECIAL_FILE")
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, "rb") as stream:
                        opened = os.fstat(stream.fileno())
                        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                            raise ValueError("CANDIDATE_CHANGED_DURING_CAPTURE")
                        data = stream.read(self.max_bytes - total + 1)
                        after = os.fstat(stream.fileno())
                    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                        raise ValueError("CANDIDATE_CHANGED_DURING_CAPTURE")
                    total += len(data)
                    if total > self.max_bytes or len(manifest) >= 10000:
                        raise ValueError("CANDIDATE_SIZE_LIMIT")
                    digest = hashlib.sha256(data).hexdigest()
                    blob = staging / digest
                    if not blob.exists():
                        blob.write_bytes(data)
                        blob.chmod(0o400)
                    manifest[relative] = {
                        "blob": digest,
                        "executable": bool(before.st_mode & 0o111),
                    }
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
        for name, entry in manifest.items():
            if not self._safe(name):
                raise ValueError("CANDIDATE_UNSAFE_PATH")
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
        if any(target.iterdir()):
            raise ValueError("MATERIALIZATION_TARGET_NOT_EMPTY")
        for name, entry in manifest.items():
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
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
