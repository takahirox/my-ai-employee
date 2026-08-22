from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from ai_employee.domain.v2 import ArtifactDescriptor, ArtifactPutRequest
from ai_employee.serialization import canonical_json

from ._common import identifier, now


class AtomicArtifactStore:
    """Write-once content-addressed storage with separately published metadata."""

    def __init__(self, root: str | Path, *, maximum_bytes: int = 16_000_000) -> None:
        if maximum_bytes < 1:
            raise ValueError("maximum_bytes must be positive")
        self.root = Path(root).resolve()
        self.maximum_bytes = maximum_bytes
        self.content_root = self.root / "sha256"
        self.metadata_root = self.root / "metadata"
        self.temporary_root = self.root / "tmp"
        for path in (self.content_root, self.metadata_root, self.temporary_root):
            path.mkdir(parents=True, exist_ok=True)

    def put(self, stream: BinaryIO, request: ArtifactPutRequest) -> ArtifactDescriptor:
        digest = hashlib.sha256()
        size = 0
        fd, temporary_name = tempfile.mkstemp(prefix="put-", dir=self.temporary_root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as output:
                while True:
                    chunk = stream.read(128 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.maximum_bytes:
                        raise ValueError("artifact exceeds configured byte limit")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            artifact_digest = digest.hexdigest()
            destination = self._content_path(artifact_digest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if self._digest_file(destination) != (artifact_digest, size):
                    raise OSError(
                        "existing content does not match its content address"
                    ) from None
            temporary.unlink()
            descriptor = ArtifactDescriptor(
                id=identifier("artifact"),
                run_id=request.run_id,
                created_at=now(),
                artifact_digest=artifact_digest,
                media_type=request.media_type,
                size_bytes=size,
                logical_kind=request.logical_kind,
                producer_action_id=request.producer_action_id,
                source=request.source,
                redaction_state="redacted" if request.redacted else "none",
                store_locator=f"sha256/{artifact_digest[:2]}/{artifact_digest}",
            )
            self._publish_metadata(descriptor)
            return descriptor
        finally:
            temporary.unlink(missing_ok=True)

    def open_verified(self, descriptor: ArtifactDescriptor) -> BinaryIO:
        expected_locator = f"sha256/{descriptor.artifact_digest[:2]}/{descriptor.artifact_digest}"
        if descriptor.store_locator != expected_locator:
            raise ValueError("artifact locator is not canonical for its digest")
        path = self._content_path(descriptor.artifact_digest)
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(128 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        if digest.hexdigest() != descriptor.artifact_digest or size != descriptor.size_bytes:
            raise OSError("artifact verification failed")
        return path.open("rb")

    def _content_path(self, digest: str) -> Path:
        return self.content_root / digest[:2] / digest

    @staticmethod
    def _digest_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(128 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def _publish_metadata(self, descriptor: ArtifactDescriptor) -> None:
        path = self.metadata_root / f"{descriptor.id}.json"
        payload = canonical_json(descriptor).encode("utf-8")
        fd, name = tempfile.mkstemp(prefix="metadata-", dir=self.temporary_root)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def metadata(self, descriptor: ArtifactDescriptor) -> dict[str, object]:
        value = json.loads((self.metadata_root / f"{descriptor.id}.json").read_text())
        if not isinstance(value, dict):
            raise OSError("invalid artifact metadata")
        if any(not isinstance(key, str) for key in value):
            raise OSError("invalid artifact metadata keys")
        return {str(key): item for key, item in value.items()}
