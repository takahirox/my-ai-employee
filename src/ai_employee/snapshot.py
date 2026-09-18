"""Shared, stdlib-only snapshot contract, also executed in the quiesced container."""

from __future__ import annotations

import io
import json
import os
import stat
import tarfile
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

LEGACY_SNAPSHOT_BYTES = 64_000_000
INIT_SNAPSHOT_BYTES = 512 * 1024**2
# Preserve the existing transport metadata allowance (80 MB minus 64 MB).
ARCHIVE_METADATA_BYTES = 16_000_000

PROTECTED = frozenset({".git", ".codex", ".claude", ".fleet", ".agents", ".fleet-inputs"})


def check_bytes(
    observed: int, limit: int, *, code: str = "CANDIDATE_SIZE_LIMIT", partial: bool = True
) -> None:
    if observed > limit:
        raise ValueError(
            f"{code}: limit_bytes={limit}, observed_bytes={observed}, "
            f"count={'partial' if partial else 'complete'}"
        )


def archive_limit(content_limit: int) -> int:
    """Bound tar headers, padding, paths and extended metadata in addition to content."""
    return content_limit + ARCHIVE_METADATA_BYTES


def safe_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and bool(path.parts)
        and not any(part in {"", ".", ".."} | PROTECTED for part in path.parts)
        and str(path) == name
        and "\x00" not in name
    )


def link_error(name: str, reason: str) -> ValueError:
    # Do not include the target: it may contain an unrelated host path.
    return ValueError(f"SNAPSHOT_SYMLINK: {json.dumps(name)}: {reason}")


def validate_entries(entries: Mapping[str, Mapping[str, Any]], *, inputs: bool = False) -> None:
    """Validate links lexically against selected entries, never the host filesystem.

    Read-only upstream snapshots travel under .fleet-inputs/<index>, but each
    remains its own referent namespace. They are never candidate artifact entries.
    """
    groups: dict[str, dict[str, Mapping[str, Any]]] = {}
    for name, entry in entries.items():
        scope, relative = "", name
        if inputs and name.startswith(".fleet-inputs/"):
            parts = name.split("/", 2)
            if len(parts) != 3 or not parts[1].isdigit():
                raise ValueError("CANDIDATE_UNSAFE_PATH")
            scope, relative = "/".join(parts[:2]) + "/", parts[2]
        if not safe_path(relative):
            raise ValueError("CANDIDATE_UNSAFE_PATH")
        groups.setdefault(scope, {})[relative] = entry
    for scope, selected in groups.items():
        directories = {str(p) for n in selected for p in PurePosixPath(n).parents}
        for name, entry in selected.items():
            if name in directories:
                raise link_error(scope + name, "entry is also a parent directory")
            if "target" not in entry:
                continue
            target = entry["target"]
            if not isinstance(target, str) or not target or "\x00" in target:
                raise link_error(scope + name, "invalid target")
            if target.startswith("/"):
                raise link_error(scope + name, "absolute target")
            parts = list(PurePosixPath(name).parent.parts)
            # Preserve component order: normalizing link/.. could hide traversal
            # through a symlink or an absent/non-directory intermediate component.
            components = target.split("/")
            for index, part in enumerate(components):
                if part == "..":
                    if not parts:
                        raise link_error(scope + name, "target escapes snapshot")
                    parts.pop()
                elif part not in {"", "."}:
                    if part in PROTECTED:
                        raise link_error(scope + name, "excluded target")
                    parts.append(part)
                resolved = "/".join(parts) or "."
                if index < len(components) - 1 and resolved not in directories:
                    raise link_error(
                        scope + name, "intermediate target is not a snapshot directory"
                    )
            referent = selected.get("/".join(parts))
            if referent is None:
                raise link_error(scope + name, "target is not a selected regular file")
            if "target" in referent:
                raise link_error(scope + name, "link chains and cycles are unsupported")


def read_entries(
    root: Path, limit: int, *, names: Iterable[str] | None = None, inputs: bool = False
) -> dict[str, dict[str, Any]]:
    """Read bounded selected bytes without following links, including in ancestors."""
    if names is None:
        selected: list[str] = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            excluded = PROTECTED - (
                {".fleet-inputs"} if inputs and Path(directory) == root else set()
            )
            dirs[:] = sorted(n for n in dirs if n not in excluded)
            links = [n for n in dirs if (Path(directory) / n).is_symlink()]
            dirs[:] = [n for n in dirs if n not in links]
            selected.extend(
                (Path(directory) / n).relative_to(root).as_posix()
                for n in sorted(files + links)
                if n not in excluded
            )
        names = selected
    entries: dict[str, dict[str, Any]] = {}
    total = 0
    for name in sorted(set(names)):
        # Validate entry names before any filesystem access (targets follow later).
        validate_entries({name: {}}, inputs=inputs)
        path = PurePosixPath(name)
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                for part in path.parts[:-1]:
                    try:
                        child = os.open(
                            part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                        )
                    except FileNotFoundError:
                        raise
                    except OSError as error:
                        raise link_error(name, "unsafe or unavailable parent directory") from error
                    os.close(parent)
                    parent = child
            except FileNotFoundError:
                # Git may also contain files beneath a deleted directory.
                continue
            try:
                before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                # Git may contain a tracked file deleted in the working tree.
                continue
            if stat.S_ISLNK(before.st_mode):
                target = os.readlink(path.name, dir_fd=parent)
                entries[name] = {"target": target}
                total += len(os.fsencode(target))
            elif stat.S_ISREG(before.st_mode):
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                with os.fdopen(fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        raise ValueError("CANDIDATE_CHANGED_DURING_CAPTURE")
                    data = stream.read(max(0, limit - total) + 1)
                    after = os.fstat(stream.fileno())
                    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                        raise ValueError("CANDIDATE_CHANGED_DURING_CAPTURE")
                entries[name] = {"data": data, "executable": bool(before.st_mode & 0o111)}
                total += len(data)
            else:
                raise ValueError("CANDIDATE_SPECIAL_FILE")
            after = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if (before.st_ino, before.st_mtime_ns, before.st_size, before.st_mode) != (
                after.st_ino,
                after.st_mtime_ns,
                after.st_size,
                after.st_mode,
            ):
                raise ValueError("CANDIDATE_CHANGED_DURING_CAPTURE")
        finally:
            os.close(parent)
        check_bytes(total, limit)
        if len(entries) > 10000:
            raise ValueError("CANDIDATE_SIZE_LIMIT")
    validate_entries(entries, inputs=inputs)
    return entries


def pack_workspace(root: Path, limit: int, *, workspace_limit: int | None = None) -> bytes:
    entries = read_entries(root, limit, inputs=True)
    if workspace_limit is not None:
        size = sum(
            len(os.fsencode(e["target"])) if "target" in e else len(e["data"])
            for e in entries.values()
        )
        check_bytes(size, workspace_limit, code="WORKSPACE_SIZE_LIMIT", partial=False)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        directories = sorted({str(p) for n in entries for p in PurePosixPath(n).parents} - {"."})
        for name in directories:
            info = tarfile.TarInfo(name)
            info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o755, 1000, 1000
            archive.addfile(info)
        for name, entry in sorted(entries.items(), key=lambda item: "target" in item[1]):
            info = tarfile.TarInfo(name)
            info.uid = info.gid = 1000
            if "target" in entry:
                info.type, info.linkname = tarfile.SYMTYPE, entry["target"]
                archive.addfile(info)
            else:
                info.size = len(entry["data"])
                info.mode = 0o755 if entry["executable"] else 0o644
                archive.addfile(info, io.BytesIO(entry["data"]))
    data = output.getvalue()
    check_bytes(
        len(data), archive_limit(limit), code="CANDIDATE_TRANSPORT_SIZE_LIMIT", partial=False
    )
    return data


def unpack_workspace(data: bytes, root: Path, limit: int) -> None:
    """Validate the entire untrusted archive before creating files, then links."""
    check_bytes(
        len(data), archive_limit(limit), code="CANDIDATE_TRANSPORT_SIZE_LIMIT", partial=False
    )
    entries: dict[str, dict[str, Any]] = {}
    directories: set[str] = set()
    seen: set[str] = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            name = member.name
            if name in seen:
                raise ValueError("CANDIDATE_TRANSPORT_DUPLICATE")
            seen.add(name)
            # Upstream parent directories have no file entry to validate.
            if name != ".fleet-inputs" and not (
                name.startswith(".fleet-inputs/")
                and name.split("/")[1].isdigit()
                and len(name.split("/")) == 2
            ):
                validate_entries({name: {}}, inputs=True)
            elif not member.isdir():
                raise ValueError("CANDIDATE_TRANSPORT_UNSAFE_PATH")
            if member.isdir():
                directories.add(name)
                continue
            if member.issym():
                entry: dict[str, Any] = {"target": member.linkname}
                total += len(os.fsencode(member.linkname))
            elif member.isfile():
                total += member.size
                check_bytes(total, limit)
                stream = archive.extractfile(member)
                assert stream is not None
                entry = {"data": stream.read(), "executable": bool(member.mode & 0o111)}
            else:
                raise ValueError("CANDIDATE_TRANSPORT_UNSAFE_TYPE")
            entries[name] = entry
            check_bytes(total, limit)
            if len(entries) > 10000:
                raise ValueError("CANDIDATE_SIZE_LIMIT")
    validate_entries(entries, inputs=True)
    for name in directories:
        if any(
            str(parent) in entries for parent in (PurePosixPath(name), *PurePosixPath(name).parents)
        ):
            raise ValueError("CANDIDATE_TRANSPORT_UNSAFE_PATH")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or any(root.iterdir()):
        raise ValueError("MATERIALIZATION_TARGET_NOT_EMPTY")
    for name, entry in sorted(entries.items(), key=lambda item: "target" in item[1]):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if "target" in entry:
            destination.symlink_to(entry["target"])
        else:
            destination.write_bytes(entry["data"])
            destination.chmod(0o700 if entry["executable"] else 0o600)
