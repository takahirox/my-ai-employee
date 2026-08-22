# My AI Employee

`my-ai-employee` is an Apache-2.0 licensed, Python 3.11+ graph-first AI Fleet
Runtime. Its Trust Kernel follows **Deterministic Core, Probabilistic
Intelligence**: workers can propose typed results and graph revisions, while only
the deterministic runtime accepts revisions, advances authoritative state,
enforces policy and budgets, evaluates evidence, and declares completion.

The package is local-first and backend-neutral. No credentials, hosted model, or
network access are needed for the demo or test suite.

## Install and try it

```bash
python -m pip install -e '.[dev]'
fleet --version
fleet demo --db /tmp/fleet-demo.db --run-id readme-demo
fleet inspect readme-demo --db /tmp/fleet-demo.db
fleet replay readme-demo --db /tmp/fleet-demo.db
fleet run examples/demo_graph.yaml --db /tmp/fleet-demo.db --run-id declarative-demo
fleet compare readme-demo declarative-demo --db /tmp/fleet-demo.db
fleet serve --db /tmp/fleet-demo.db
```

The Inspector binds to `127.0.0.1:8765` by default. `AUTO_MERGE_ELIGIBLE` is an
advisory structured decision; Fleet never invokes Git merge or bypasses required
human approval.

## v0.2 reviewed-patch workflow

v0.2 adds an opt-in, local `fleet work` path. A subscription-authenticated
Codex CLI (primary) or Claude Code CLI produces a strict proposal envelope. The
runtime—not the model—resolves policy, applies declared unified diffs inside a
sibling Git worktree, mediates commands/downloads/installs, runs project checks,
captures an exact patch digest, and waits for explicit promotion approval.

Copy and review the example Harness in the repository you want Fleet to work on:

```bash
mkdir -p /path/to/project/.fleet
cp examples/project-v2/.fleet/project.yaml /path/to/project/.fleet/project.yaml
fleet project /path/to/project
fleet work "Make one small objectively verifiable improvement" \
  --repo /path/to/project --worker codex_cli --db /tmp/fleet-work.db --json
```

The selected CLI must already be installed and authenticated by its own official
login flow. Fleet does not read or store its credential. Codex is invoked in a
read-only proposal sandbox; edits are returned as typed unified diffs and applied
by Fleet only after path and policy checks.

The Claude Code adapter currently disables Claude tools completely, so it is most
useful when sufficient bounded context is already present in the goal. Codex CLI is
the source-aware primary adapter for v0.2. Direct OpenAI or Anthropic API keys are
neither required nor supported by this release.

Inspect and control a run from another terminal:

```bash
fleet inspect RUN_ID --db /tmp/fleet-work.db
fleet logs RUN_ID --db /tmp/fleet-work.db
fleet approvals list --run RUN_ID --db /tmp/fleet-work.db
fleet approvals approve APPROVAL_ID --request-digest REQUEST_DIGEST --db /tmp/fleet-work.db
fleet resume RUN_ID --db /tmp/fleet-work.db
fleet diff RUN_ID --db /tmp/fleet-work.db
fleet promote RUN_ID --patch-digest PATCH_DIGEST --db /tmp/fleet-work.db
```

`fleet promote` is the only v0.2 operation that applies the reviewed patch to the
original worktree. Fleet never commits, pushes, publishes, or deploys it.

Network retrieval and project-local installation are supported only through
typed, digest-bound services. The example Harness disables both. Enabling them
requires an explicit domain/ecosystem policy and may require a digest-bound human
approval. Host-global installation remains denied.

## What the Trust Kernel contains

- strict, serializable domain models and explicit Run/Task/Node transition tables;
- deterministic graph validation, immutable accepted revisions, and generation fences;
- bounded in-process scheduling for System, Function, Predicate, Gate, and
  Process-result nodes, including retry/repair loops, pause, cancellation, and resume;
- versioned output contracts, role-scoped context compilation, evidence coverage,
  completion and review decisions;
- `.fleet/` Project Harness discovery, safe v1 migration, and provisional inference;
- fixed, policy, and explainable adaptive routing with conservative history handling;
- vendor-neutral events and SQLite persistence, deterministic no-worker replay, and a
  small read-only Inspector;
- Python graph construction plus strict JSON/YAML loading.
- v0.2 controlled process, HTTPS download, project-local install, worktree edit,
  approval, verification, evidence, Inspector, and exact-digest promotion services.

## Documentation

- [Architecture and authority boundaries](docs/architecture.md)
- [Project Harness guide](docs/project-harness.md) and
  [example profile](examples/project/.fleet/project.yaml)
- [OpenAI Agents SDK and TAKT influences](docs/influences.md)
- [Security, permissions, sandboxing, and secrets](docs/security.md)
- [Development and release checks](docs/development.md)
- [v0.2/v0.3 roadmap](docs/roadmap.md)
- [Normative v0.2 specification](docs/v0.2-spec.md)
- [Declarative graph example](examples/demo_graph.yaml)

## Python API

```python
from ai_employee.demo import demo_graph, demo_goal
from ai_employee.domain import ExecutionPolicy
from ai_employee.graph import accept_graph

graph = demo_graph()
accepted = accept_graph(
    graph,
    ExecutionPolicy(max_nodes=10, max_attempts=8, max_wall_seconds=60.0),
)
print(accepted.revision_number, accepted.content_digest)
```

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run mypy
uv run python -m build
```

Python 3.11, 3.12, and 3.13 are supported. See [CONTRIBUTING.md](CONTRIBUTING.md)
and [SECURITY.md](SECURITY.md) before submitting changes or reporting
vulnerabilities.
