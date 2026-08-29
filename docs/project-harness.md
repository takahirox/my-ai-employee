# Project Harness

The Project Harness is a declarative, repository-owned description of commands, rules,
protected paths, contracts, and verification expectations. Fleet looks for
`.fleet/project.yaml`, `.fleet/project.yml`, then `.fleet/project.json`. Loading is
non-destructive: if none exists, `discover_project_profile()` returns a provisional in-memory
profile and does not write into the project.

Start with `examples/project/.fleet/project.yaml`. Keep commands deterministic and suitable
for the repository's sandbox. Rules inferred from files remain provisional until a maintainer
reviews and writes an explicit profile. Protected paths express boundaries; they do not grant
write authority or replace OS-level sandboxing.

Parse a profile through the public API:

```python
from ai_employee.project import discover_project_profile

profile = discover_project_profile("examples/project")
print(profile.id, profile.commands)
```

Schema version 2 can declare developer-managed first-party evaluators separately from
commands:

```yaml
commands:
  test:
    argv: ["uv", "run", "pytest", "-q"]
    cwd: "."
evaluators:
  - id: unit-tests
    provider_id: process.harness
    command_ref: test
    criterion_ids: [tests-pass]
verification:
  required: [test]
  required_evaluators: [unit-tests]
```

`process.harness` may reference only a command declared in the same Harness. Evaluator IDs,
criterion IDs, required references, command references, and providers are validated strictly.
Harness declarations express repository intent but grant no capabilities: built-in and
operator policy still mediate process execution, cancellation, artifact limits, and budgets.
Provisional Harnesses cannot declare evaluators.

The graph-first `fleet work` path executes every item in
`verification.required_evaluators` against the exact composed parent candidate before it can
become `ready_to_promote`. Requests, process results, observation manifests, typed criterion
results/findings, evidence ledgers, and the parent decision are persisted with candidate,
generation, Harness, evaluator-specification, and effective-policy digest bindings. Inspector
projects those records without opening artifact bodies, and replay reads the stored decision
without re-running a worker, process, composition, or promotion action.

For backward compatibility, discovery derives one required `process.harness` evaluator for
each legacy entry in `verification.required` when no explicit `required_evaluators` are
declared. Projects that use only the established command list therefore keep the same
verification intent while gaining exact-candidate evidence binding.

`browser.playwright`, `judge.visual`, and `threejs.instrumentation` remain stable reserved IDs
for later first-party providers. They are not available implementations, so declaring them
currently fails closed. Harnesses cannot load third-party providers, Python entry points, or
dynamic imports. An unsatisfied deterministic process evaluation is persisted as typed
`REPAIR` evidence, but the parent candidate currently fails closed; it does not start an
automatic repair worker.
