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

To keep repository context on the local machine, Fleet can use an already-installed
Ollama model directly. For example:

```bash
fleet work "Create a reviewed design note" --repo /path/to/project \
  --worker ollama_cli --model qwen3-coder:30b \
  --db /tmp/fleet-local.db --json
```

The named model must already be available in local Ollama; Fleet
does not implicitly download it.

Worker executable locations are machine-specific operator configuration, not
version-controlled Project Harness policy. By default Fleet optionally loads
`~/.config/my-ai-employee/config.yaml`; use `--operator-config PATH` or the
`MY_AI_EMPLOYEE_CONFIG` environment variable to select another file:

Task-aware adaptive routing is the default for `fleet work`. When the operator does
not provide routing configuration, the built-in cloud-only `codex-balanced` set is
used. Operator configuration can replace each exact strategy ID, backend, model,
effort, capability, and complexity/scale/risk bound. The Project Harness can only
narrow allowed IDs/backends and must opt into adaptive or local routing. Named
strategy sets provide reproducible evaluation profiles without duplicating model
definitions. Explicit worker/model selection remains available through
`--routing-mode legacy`.

```bash
fleet work "Fix the bug" --repo /path/to/project --routing-mode fixed --strategy codex-luna-max
fleet work "Fix the bug" --repo /path/to/project --routing-mode adaptive
fleet work "Fix the bug" --repo /path/to/project --routing-mode adaptive --strategy-set codex-balanced
fleet work "Fix the bug" --repo /path/to/project --routing-mode adaptive --strategy-set claude-only
fleet work "Fix the bug" --repo /path/to/project --routing-mode adaptive --strategy-set codex-claude
fleet work "Fix the bug" --repo /path/to/project --routing-mode adaptive --strategy-set local-only
fleet work "Fix the bug" --repo /path/to/project --assessment-strategy codex-sol-high
fleet work "Fix the bug" --repo /path/to/project --planner-strategy codex-sol-high
fleet work "Fix the bug" --repo /path/to/project --routing-mode legacy --worker codex_cli --model MODEL
```

Adaptive routing first obtains a repository-isolated strict-JSON semantic assessment. The
built-in assessment strategy is `gpt-5.6-sol` with `high` effort; operators may replace
the default or select an exact authorized strategy with `--assessment-strategy`.
The tool-disabled classifier returns only a categorical profile: task type (`mechanical`,
`retrieval`, `diagnosis`, `implementation`, `architecture`, `research`, `planning`, or
`open_ended_strategy`), reasoning class (`mechanical`, `simple`, `moderate`, `deep`, or
`open_ended`), scope (`bounded`, `local`, `multi_component`, or `broad`), ambiguity
(`low`, `medium`, or `high`), and bounded reasons. It cannot return risk, capabilities,
strategy, model, effort, cost, policy, or a routing decision.
Each strategy set may define its own assessor. The built-in `claude-only` set uses
`claude-fable-5` with `high` effort for assessment, then routes ordinary low-risk work
to `claude-opus-5`/`high` and work outside the Opus bounds to
`claude-fable-5`/`high`. This keeps every model call in that profile on Claude.
Deterministic code maps task-type floors to `1/2/4/3/7/6/4/9`, reasoning floors to
`1/2/4/7/9`, and ambiguity floors to `1/4/7`; complexity is their maximum. Scope maps
to scale as `bounded=1`, `local=2`, `multi_component=5`, and `broad=8`. These numeric
bands preserve existing strategy configuration compatibility and contain no model-authored
score or hidden weight. Harness-derived risk and effective capabilities remain independent
mandatory floors. Normalized goal length is persisted as `context_character_count` evidence
but never affects eligibility, fit, headroom, or tie-breaking. Assessment failure is
fail-closed and never falls back to another model or Local LLM.

Routing never automatically falls back to another model, backend, or local
strategy; an unsatisfied selection fails closed. Routed execution binds the configured
model and effort for Codex (`--model` plus `model_reasoning_effort`), Claude
(`--model` plus `--effort`), and Ollama (`run MODEL` plus `--think`). A Local strategy
still requires both an operator-defined set and Project Harness `local_backend: true`.

Inspector persists the strategy-set name, assessment strategy, merged task assessment,
and selected execution strategy for evaluation. Decomposition is top-level assessment
data only, not a set of independently executed subtasks.
In adaptive mode, Fleet uses the same bound semantic profile to select the graph Planner
deterministically from strategies explicitly marked `planner_eligible` by the operator. Candidate
strategies pass through the configured strategy set, Project Harness IDs and backends, cloud-only
Planner boundary, required capabilities, risk, and compatibility bounds before selection. The
complete candidate and eligible sets, profile, assessor, selected Planner and routing reasons are
digest-bound to the ProposedGraph and persisted GraphRun. `--planner-strategy` retains exact fixed
Planner selection while enforcing the same eligibility constraints.
Adaptive planner routing fields are persisted as non-authoritative hints. After graph
acceptance, each node that will use adaptive routing receives an independent tool-disabled
semantic assessment from the configured assessment strategy. Fleet binds that assessment to
the accepted revision and deterministically merges policy, Harness, risk, capability,
dependency, completion, and context facts before strategy selection. Replay and resume reuse
the bound record without invoking AI. Fixed, policy, hand-authored, and compatible deterministic
graphs retain their stored bands and do not receive an additional assessment.

An explicit Project Harness and operator double opt-in can also enable tool-disabled semantic
review of the exact composed parent patch after deterministic verification. The reviewer produces
typed, digest-bound evidence only; the deterministic Trust Kernel retains final
`PASS`/`REPAIR`/`ESCALATE`/`FAIL` authority. See the
[Issue 8 design review](docs/issue-8-review.md).

When both `--routing-mode` and `--strategy-set` are omitted, adaptive routing uses the
operator-configured `default_strategy_set`. The built-in and example default is
`codex-balanced`: ordinary, low-risk work uses `gpt-5.6-luna` with `max` effort,
while work outside its configured bounds uses `gpt-5.6-sol` with `high` effort.

Configured executable paths must be absolute. If no entry exists, Fleet retains
the backward-compatible deterministic `PATH` lookup for `codex` or `claude`. The
effective executable is recorded with worker availability provenance. See the
complete, updated [`operator config`](examples/operator-config.yaml) and
[`Project Harness`](examples/project-v2/.fleet/project.yaml) examples.

The Claude Code adapter currently disables Claude tools completely, so it is most
useful when sufficient bounded context is already present in the goal. Codex CLI is
the source-aware primary adapter for v0.2. Direct OpenAI or Anthropic API keys are
neither required nor supported by this release.

Inspect and control a run from another terminal:

```bash
fleet inspect RUN_ID --db /tmp/fleet-work.db
fleet explain RUN_ID --db /tmp/fleet-work.db
fleet logs RUN_ID --db /tmp/fleet-work.db
fleet approvals list --run RUN_ID --db /tmp/fleet-work.db
fleet approvals approve APPROVAL_ID --request-digest REQUEST_DIGEST --db /tmp/fleet-work.db
fleet resume RUN_ID --db /tmp/fleet-work.db
fleet diff RUN_ID --db /tmp/fleet-work.db
fleet promote RUN_ID --patch-digest PATCH_DIGEST --db /tmp/fleet-work.db
```

Promotion approval is manual by default. A bounded low-risk graph parent candidate can receive a
durable policy approval only with the Project Harness and operator double opt-in described in
[the policy-controlled approval guide](docs/issue-9-auto-approval.md). Policy approval never runs
`fleet promote`; promotion remains an explicit, exact-digest operation.

`fleet promote` is the only v0.2 operation that applies the reviewed patch to the
original worktree. Fleet never commits, pushes, publishes, or deploys it.

`fleet explain` builds a deterministic, read-only story for one running or historical
run from its persisted facts. It summarizes the Goal, current task positions, graph
evolution, routing reasons, body-free information flow, evidence and review decisions,
failure path, and final disposition. It never invokes an AI worker, reads artifact
bodies, or mutates/migrates the database. `fleet inspect` remains the detailed raw
forensic projection. The local Inspector exposes the same summary at
`/api/runs/RUN_ID/explanation`.

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
