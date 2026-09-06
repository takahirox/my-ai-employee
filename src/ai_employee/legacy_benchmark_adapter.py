"""Historical proposal-mode adapter; optional pocket-agent-bench dependency."""

import json
import shlex
import tempfile
import time
from pathlib import Path

from pocket_bench.agents import CodexAgent


class FleetAgent(CodexAgent):
    command_policy = "readonly-network"

    @staticmethod
    def name():
        return "my-ai-employee"

    def version(self):
        return "0.2.1/snapshot-recorded-in-runtime-manifest"

    async def setup(self, environment):
        installed = await environment.exec(
            command='python -c "import importlib.util; '
            "assert importlib.util.find_spec('ai_employee') is not None\"",
            user="agent",
        )
        if installed.return_code:
            raise RuntimeError(
                "Fleet is unavailable in this image; build_runtime.py --fleet is required"
            )
        await super().setup(environment)
        await environment.exec(
            command="mkdir -p /.fleet-app; chown agent:agent /.fleet-app", user="root"
        )
        if self.mode != "single":
            raise ValueError(
                "Fleet adapter exposes fixed single-node mode; team comparison uses CodexAgent"
            )
        project = {
            "schema_version": 2,
            "commands": {"smoke": {"argv": ["python", "/opt/pocket/smoke.py"], "cwd": "."}},
            "paths": {
                "writable": ["output/**", "src/**"],
                "protected": ["input/**", ".git/**", ".fleet/**"],
            },
            "verification": {"required": ["smoke"], "review": {"required": False}},
            "worker": {
                "allowed": ["codex_cli"],
                "allowed_strategy_ids": ["bench"],
                "adaptive_routing": False,
            },
            "budgets": {"wall_seconds": self.agent_seconds, "worker_turns": 3, "processes": 12},
        }
        operator = {
            "schema_version": 1,
            "workers": {
                "codex_cli": {
                    "executable": "/usr/local/bin/pocket-codex",
                    "path_entries": ["/usr/local/bin", "/usr/bin", "/bin"],
                }
            },
            "routing": {
                "strategies": [
                    {
                        "id": "bench",
                        "backend": "codex_cli",
                        "model": self.model_name or "gpt-5.6-luna",
                        "effort": self.effort,
                        "capabilities": ["edit_intent", "process"],
                    }
                ]
            },
        }
        with tempfile.TemporaryDirectory(prefix="pocket-config-") as d:
            await environment.upload_file(
                Path(__file__).with_name("fleet_wrapper.py"), "/usr/local/bin/pocket-codex"
            )
            await environment.exec(command="chmod 755 /usr/local/bin/pocket-codex", user="root")
            for name, value, target in (
                ("project.json", project, "/app/.fleet/project.yaml"),
                ("operator.json", operator, "/home/agent/operator.yaml"),
            ):
                p = Path(d) / name
                p.write_text(json.dumps(value))
                await environment.exec(command="mkdir -p /app/.fleet", user="agent")
                await environment.upload_file(p, target)
        await environment.exec(
            command="chown -R agent:agent /app/.fleet /home/agent/operator.yaml", user="root"
        )
        await environment.exec(
            command="git add .fleet && git commit -qm harness", cwd="/app", user="agent"
        )

    async def run(self, instruction, environment, context):
        started = time.time()
        try:
            cmd = shlex.join(
                [
                    "fleet",
                    "work",
                    instruction,
                    "--repo",
                    "/app",
                    "--routing-mode",
                    "fixed",
                    "--strategy",
                    "bench",
                    "--operator-config",
                    "/home/agent/operator.yaml",
                    "--non-interactive",
                    "--json",
                    "--db",
                    "/home/agent/run/state.db",
                ]
            )
            r = await environment.exec(
                command=cmd + " > /logs/agent/fleet.json 2> /logs/agent/fleet.stderr",
                user="agent",
                timeout_sec=self.agent_seconds,
            )
            self.events.append(
                {
                    "role": "fleet",
                    "exit_code": r.return_code,
                    "started_at": started,
                    "duration_seconds": time.time() - started,
                }
            )
            r = await environment.exec(command="cat /logs/agent/fleet.json", user="agent")
            try:
                result = json.loads(r.stdout or "{}")
            except ValueError:
                result = {}
            if result.get("run_id"):
                inspect = shlex.join(
                    ["fleet", "inspect", result["run_id"], "--db", "/home/agent/run/state.db"]
                )
                await environment.exec(
                    command=inspect
                    + " > /logs/agent/fleet-inspect.json 2> /logs/agent/inspect.stderr",
                    user="agent",
                )
                # Materialize only in the disposable task, never promote user source.
                cmd = shlex.join(
                    ["fleet", "diff", result["run_id"], "--db", "/home/agent/run/state.db"]
                )
                r = await environment.exec(
                    command=cmd + " > /logs/agent/candidate.patch", user="agent"
                )
                if r.return_code == 0:
                    applied = await environment.exec(
                        command="git apply /logs/agent/candidate.patch", cwd="/app", user="agent"
                    )
                    if applied.return_code:
                        raise RuntimeError(
                            "Fleet candidate materialization failed: " + (applied.stderr or "")
                        )
            await self.execute_declared(environment)
        except Exception as e:
            if not self.events:
                self.events.append(
                    {
                        "role": "fleet",
                        "error": type(e).__name__,
                        "started_at": started,
                        "duration_seconds": time.time() - started,
                    }
                )
            raise
        finally:
            await self.collect(environment, context)
            context.metadata["fleet_usage_note"] = (
                "Native worker JSONL events are collected by a transparent executable wrapper."
            )
