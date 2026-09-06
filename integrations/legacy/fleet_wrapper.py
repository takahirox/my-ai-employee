#!/usr/local/bin/python
"""Historical wrapper: preserve the structured response and collect native usage."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, "/opt/pocket")
from codex_policy import fleet_exec_args

env = dict(
    os.environ,
    HTTPS_PROXY="http://model-proxy:3128",
    HTTP_PROXY="http://model-proxy:3128",
    NO_PROXY="localhost,127.0.0.1",
)
name = "fleet-worker-" + uuid.uuid4().hex
final = Path("/logs/agent/" + name + ".txt")
is_model = "exec" in sys.argv[1:]
args = fleet_exec_args(sys.argv[1:], final)
if is_model:
    # Never persist the prompt or credentials in this policy-only evidence.
    Path("/logs/agent/" + name + ".policy.json").write_text(
        json.dumps(
            {
                "policy": "readonly-network",
                "filesystem": "read-only",
                "network": True,
            }
        )
    )
with (
    open("/logs/agent/" + name + ".jsonl", "wb") as log,
    open("/logs/agent/" + name + ".stderr", "wb") as err,
):
    proc = subprocess.Popen(
        ["/usr/local/bin/codex", *args], stdout=subprocess.PIPE, stderr=err, env=env
    )
    for line in proc.stdout:
        log.write(line)
        log.flush()
        if not is_model:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
    code = proc.wait()
    if is_model and final.exists():
        sys.stdout.buffer.write(final.read_bytes())
        sys.stdout.buffer.flush()
    sys.exit(code)
