# Strategy evaluation first milestone

Issue #1 is implemented as a deliberately small experiment layer over existing Fleet authority.
It does not create a second verification or telemetry system.

## Evaluation contract

The unit is `Scenario x Strategy x Trial`. A scenario fixes one clean Git repository, its exact
current commit, one goal, and the ordered commands already required by Project Harness. Strategies
are exact IDs from operator configuration and run in `fixed` routing mode. Fleet admits at most
eight strategies, twenty trials per strategy, and forty total trials in one experiment.

Before and after every trial, Fleet verifies the fixture HEAD, clean status, Harness digest, and
operator-config digest. The existing work runtime creates the isolated worktree; the evaluation
layer never applies or promotes a candidate into the source fixture. A changed input fails the
trial closed.

An example scenario is JSON-compatible YAML:

```yaml
schema_version: "2"
id: small-refactor
repository_fixture: ../fixture
base_commit: 0123456789abcdef0123456789abcdef01234567
goal: Remove the duplicated parser without changing behavior
verification_commands:
  - schema_version: "2"
    name: test
    argv: [uv, run, pytest, -q]
    cwd: .
    inherit_environment: []
tags: [refactor, simple]
```

The commit and commands must exactly match the fixture and Harness at invocation time. Run a
bounded comparison with exact operator strategies:

```console
fleet eval evals/small-refactor.yaml \
  --strategy codex-luna-max \
  --strategy codex-sol-high \
  --trials 5 \
  --db .fleet/evals.db
```

Use `--json` for canonical JSON. The default output is a short table. The deterministic experiment
ID can be passed to `fleet inspect EXPERIMENT_ID --db .fleet/evals.db`; inspection reads only saved
records and invokes no worker, verifier, composer, or promotion action.

## Result authority and recovery

Verified success is not inferred from the CLI exit code. A successful trial requires the current
graph run to be `ready_to_promote`, the exact fixed strategy/base/Harness/config bindings to match,
and the parent evaluation to be `PASS`. Fleet reuses the policy auto-approval evidence validator to
replay the exact AcceptanceLedger, evaluator ledgers, manifests, runtime results, candidate, and
accepted graph bindings. Missing, foreign, stale, duplicate, or ambiguous evidence produces an
unverified stable failure.

Metrics are derived from the bound node WorkerResults, parent verification runtime records, and
the digest-verified parent candidate artifact:

- verified success rate, whose denominator is every planned trial;
- total, worker, and verification duration;
- input/output tokens and cost only when every bound worker result supplies a finite non-negative
  value (otherwise `null`, never zero-filled);
- attempts and approvals required;
- changed files, changed patch lines, patch bytes, and stable failure code.

Scenario, experiment, running trial, result, and completed trial facts are inserted with immutable
identities. Re-running the same plan skips valid completed results. A crash after result persistence
finishes the stored trial without invoking a worker. A running orphan with no terminal evidence is
recorded indeterminate and is never automatically retried; graph-level crash resumption remains a
separate concern.

## Explicit non-goals

This milestone does not add an LLM judge, semantic quality score, Web UI, confidence intervals,
parallel/distributed trials, cross-project analytics, frozen WorkerRequest replay, automatic
promotion, or automatic adaptive-routing updates. `ExecutionMetrics` and `StrategyPerformance`
remain routing/observability records and are not evaluation authority.
