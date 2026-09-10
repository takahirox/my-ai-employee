"""Native-session lifetime guard (stdlib only, launched with isolated Python).

The guard shares a fresh process group with its native child. If the controller
is killed, it terminates that group without relying on controller cleanup.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def main() -> int:
    parent = int(sys.argv[1])
    if os.getppid() != parent:
        return 125
    process = subprocess.Popen(sys.argv[2:])
    while process.poll() is None:
        if os.getppid() != parent:
            os.killpg(os.getpgrp(), signal.SIGKILL)
            return 125
        time.sleep(0.05)
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
