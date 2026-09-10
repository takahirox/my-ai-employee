"""Shared bounded cleanup for a runtime-owned process group."""

from __future__ import annotations

import os
import signal
import subprocess


class ProcessGroupCleanupError(RuntimeError):
    pass


def terminate_group(process: subprocess.Popen[bytes], grace_seconds: float = 1.0) -> str:
    """Retain the existing executor's graceful termination and exited-leader handling."""
    leader_exited = process.poll() is not None
    try:
        os.killpg(process.pid, signal.SIGKILL if leader_exited else signal.SIGTERM)
        if leader_exited:
            return "sigkill_confirmed"
        process.wait(timeout=grace_seconds)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return "sigterm_confirmed"
        except PermissionError:
            return "already_exited"
        return "sigkill_confirmed"
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return "sigkill_confirmed"
    except ProcessLookupError:
        return "already_exited"
    except PermissionError:
        if leader_exited or process.poll() is not None:
            return "already_exited"
        try:
            process.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            pass
        else:
            return "already_exited"
        raise ProcessGroupCleanupError("process group cleanup could not be confirmed") from None
