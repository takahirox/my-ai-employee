# My AI Employee

`my-ai-employee` is an Apache-2.0 licensed, Python 3.11+ graph-first AI Fleet
Runtime. Its v0.1 Trust Kernel follows **Deterministic Core, Probabilistic
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

## What v0.1 contains

- strict, serializable domain models and explicit Run/Task/Node transition tables;
- deterministic graph validation, immutable accepted revisions, and generation fences;
- bounded in-process scheduling for System, Function, Predicate, Gate, and
  Process-result nodes, including retry/repair loops, pause, cancellation, and resume;
- versioned output contracts, role-scoped context compilation, evidence coverage,
  completion and review decisions;
- `.fleet/` ProjectProfile discovery with provisional, non-persisted inference;
- fixed, policy, and explainable adaptive routing with conservative history handling;
- vendor-neutral events and SQLite persistence, deterministic no-worker replay, and a
  small read-only Inspector;
- Python graph construction plus strict JSON/YAML loading.

See [Architecture](docs/architecture.md), [Project Harness example](examples/project/.fleet/project.yaml),
and [declarative graph](examples/demo_graph.yaml).

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
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

Python 3.11, 3.12, and 3.13 are supported. See `CONTRIBUTING.md` and
`SECURITY.md` before submitting changes or reporting vulnerabilities.
