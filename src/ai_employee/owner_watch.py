"""Host-side ownership pipe for one disposable Docker candidate and its gateway.

This process is independent of the controller's process group. EOF means ownership
was lost, not that a work-policy duration elapsed. Only exact runtime-generated
names are accepted; no credentials, history, or workspace enter this process.
"""

from __future__ import annotations

import re
import selectors
import subprocess
import sys
import time
from pathlib import Path


def resource_missing(kind: str, name: str, error: bytes) -> bool:
    message = error.decode(errors="replace").lower()
    return f"no such {kind}: {name}" in message or (
        kind == "network" and f"network {name} not found" in message
    )


def start(name: str) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve()), name],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert process.stdin and process.stdout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(5) or process.stdout.readline() != b"ready\n":
                raise RuntimeError("OWNER_WATCH_UNAVAILABLE")
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        process.stdin.close()
        raise
    finally:
        process.stdout.close()
    return process


def watch(name: str) -> None:
    if re.fullmatch(r"fleet-candidate-[0-9a-f]{32}", name) is None:
        raise ValueError("INVALID_OWNER_RESOURCE")
    print("ready", flush=True)
    if sys.stdin.buffer.read(1) == b"D":
        return  # Owner already confirmed normal cleanup.
    # Cover Docker creation that was in flight when the owner disappeared.
    # These are control-plane cleanup bounds, never normal work deadlines.
    until = time.monotonic() + 30
    while True:
        confirmed = True
        for kind, resource in (
            ("container", name),
            ("container", name + "-proxy"),
            ("network", name + "-network"),
        ):
            try:
                result = subprocess.run(
                    ["docker", kind, "rm", *(["-f"] if kind == "container" else []), resource],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=15,
                    check=False,
                )
                if result.returncode and not resource_missing(kind, resource, result.stderr):
                    confirmed = False
            except (OSError, subprocess.TimeoutExpired):
                confirmed = False
        # Losing the daemon is not proof of release. Keep the cleanup owner alive
        # until removal/absence is confirmed, even after the creation race window.
        if time.monotonic() >= until and confirmed:
            return
        time.sleep(1)


if __name__ == "__main__":
    watch(sys.argv[1])
