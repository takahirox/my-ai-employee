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

`approvals.promotion` defaults to `required`. Setting it to `policy` is only the repository half of
the bounded promotion auto-approval opt-in; the operator must independently enable the exact
repository and limits. See [policy-controlled promotion approval](issue-9-auto-approval.md).

Independent task-result AI review is disabled by default. A repository may set
`verification.review.independent_task_review: true`, but review runs only when operator routing
also selects a `default_task_reviewer_strategy` whose strategy has
`task_reviewer_eligible: true`. This double opt-in permits the exact worker request/result and
deterministic evidence to reach that configured reviewer; it grants no repository or tool access
and sends no arbitrary artifact bodies, secrets, or conversation history.

Parent-candidate semantic review is a separate disabled-by-default gate. Set
`verification.review.parent_semantic_review: true` and separately configure an operator
`default_parent_reviewer_strategy` whose strategy has `parent_reviewer_eligible: true`. It runs
after required deterministic parent evaluators pass. The tool-disabled observer receives the exact
digest-verified composed patch plus body-free deterministic artifact descriptors; enabling it is an
explicit permission for that candidate patch body to reach the selected backend. Fleet's Trust
Kernel alone maps typed findings to `PASS`, `REPAIR`, `ESCALATE`, or `FAIL`. See
[`issue-8-review.md`](issue-8-review.md) for bindings, egress limits, replay, and repair-handoff
boundaries.

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

`browser.playwright` is an available, developer-managed first-party provider. Its typed
scenario supports bounded navigation, click, and fill actions plus screenshot, console, DOM,
and accessibility captures. It serves only files contained by the exact candidate workspace,
enforces one exact loopback origin, blocks redirects/background requests, uses an ephemeral
credential-free context, and always tears down the browser layers. Install the optional
`browser` extra and its Chromium binary only for projects that declare this provider. See
`examples/browser-evaluator` for a command-plus-browser Harness.

`judge.visual` and `threejs.instrumentation` remain reserved for later first-party providers
and fail closed today. Harnesses cannot load third-party providers, Python entry points, or
dynamic imports.

An unsatisfied deterministic parent evaluation is persisted as typed `REPAIR` evidence, but
the current parent candidate fails closed. The follow-up transition is deliberately defined
to reuse the existing bounded node repair machinery: create an immutable loop transition that
names the failed parent evaluation and evidence ledger digests, increment generation and
attempt, include only those accepted evidence references in the next worker request, recompose
a new exact candidate, and rerun all required evaluators. `max_repairs`, remaining wall/process/
action/artifact budgets, and generation fences decide whether the transition is admitted;
exhaustion escalates or fails. Probabilistic or indeterminate findings escalate and never enter
automatic repair. This contract avoids granting evaluator providers mutation or transition
authority.
