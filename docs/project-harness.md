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

The current foundation parses evaluator definitions, but `WorkCoordinator` execution is a
later milestone. A Harness that places an evaluator in `verification.required_evaluators`
therefore makes `fleet work` fail closed with `EVALUATOR_EXECUTION_UNAVAILABLE`; Fleet never
silently ignores a required evaluation. The repository example declares providers without
making them required so its existing command-verification flow remains runnable.

`browser.playwright`, `judge.visual`, and `threejs.instrumentation` are stable reserved IDs
for later first-party providers. They are not available implementations, so declaring them
currently fails closed. Harnesses cannot load third-party providers, Python entry points, or
dynamic imports.
