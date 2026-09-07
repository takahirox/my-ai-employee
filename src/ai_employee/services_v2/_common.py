from __future__ import annotations

import hashlib
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def now() -> datetime:
    return datetime.now(UTC)


def identifier(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_git_command(
    args: Sequence[str],
    *,
    input: bytes | None = None,
    env: Mapping[str, str] | None = None,
    capture_output: bool = True,
    check: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Supervise trusted internal Git independently of model process reservations."""
    from ai_employee.stage_control import StageCancellation

    if not args or args[0] != "git" or not capture_output:
        raise ValueError("internal Git boundary requires a captured Git command")
    cancellation = StageCancellation()
    cancellation.check()
    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        pending_input = input
        while True:
            cancellation.check()
            try:
                stdout, stderr = process.communicate(input=pending_input, timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                pending_input = None
        cancellation.check()
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, args, stdout, stderr)
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    finally:
        # Git hooks/filters belong to this internal operation too. Closing the
        # capture pipes or exiting its leader cannot grant a background lifetime.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def run_git(worktree: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    completed = run_git_command(
        ("git", "-C", str(worktree), *args),
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise ValueError(f"git {' '.join(args)} failed: {message}")
    return completed.stdout
