"""Disposable application test: persistent SQL counter and Redis cache round trip."""

import argparse
import json
import subprocess
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--expected", type=int, default=1)
args = parser.parse_args()
root = Path("/fleet-runtime")
pg_bin = "/usr/lib/postgresql/15/bin/"
processes = []


def command(argv, *, check=True):
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=check)


def start(argv, name):
    with (root / (name + ".log")).open("w") as log:
        process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
    processes.append(process)


try:
    if not (root / "pg").exists():
        command([pg_bin + "initdb", "-D", str(root / "pg"), "-A", "trust"])
    start(
        [pg_bin + "postgres", "-D", str(root / "pg"), "-h", "127.0.0.1", "-p", "55432", "-k", ""],
        "postgres",
    )
    start(
        [
            "redis-server",
            "--bind",
            "127.0.0.1",
            "--port",
            "56379",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(root),
        ],
        "redis",
    )
    sql = [pg_bin + "psql", "-h", "127.0.0.1", "-p", "55432", "-d", "postgres", "-tAc"]
    cache = ["redis-cli", "-h", "127.0.0.1", "-p", "56379"]
    deadline = time.monotonic() + 10
    while True:
        if (
            command([*sql, "SELECT 1"], check=False).returncode == 0
            and command([*cache, "PING"], check=False).stdout.strip() == "PONG"
        ):
            break
        if time.monotonic() >= deadline or any(p.poll() is not None for p in processes):
            raise RuntimeError("Service readiness failed")
        time.sleep(0.05)
    command([*sql, "CREATE TABLE IF NOT EXISTS items (id serial PRIMARY KEY, name text)"])
    value = command([*sql, "INSERT INTO items(name) VALUES ('fixture') RETURNING id"]).stdout
    assert int(value.splitlines()[0]) == args.expected, value
    command([*cache, "SET", "item", str(args.expected)])
    assert command([*cache, "GET", "item"]).stdout.strip() == str(args.expected)
    print(
        json.dumps(
            {
                "postgres": command([pg_bin + "postgres", "--version"]).stdout.strip(),
                "redis": command(["redis-server", "--version"]).stdout.strip(),
                "application_counter": args.expected,
            }
        ),
        flush=True,
    )
finally:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
