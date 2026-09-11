# My AI Employee repository guidance

## Design and review

- Prefer the simplest implementation that satisfies the current acceptance criteria.
  Think broadly; implement narrowly. Future usefulness alone does not justify an
  abstraction, framework, plugin system, fallback, retry, adapter or new state.
- Complexity requires evidence: an observed/reproducible failure, an explicit current
  requirement, a concrete security/correctness invariant, or an unavoidable external
  contract. Reuse or simplify the existing path first.
- A trivial task must have a trivial execution path. Activate clarification questions,
  graph growth, extra semantic review, authority setup and recovery only when needed
  by evidence or the configured policy; do not bypass mandatory verification or safety.
- Fleet owns orchestration boundaries, not the world around them: Goal/Task scheduling,
  policy/budgets, sandbox/authority, Candidate lineage, verification and durable history.
  Keep benchmark protocols and external systems' workflows outside the product runtime.
- One fact, one authority, many readers. Derive generation, validation, review and repair
  from the same contract; prompts and test conveniences cannot replace enforcement.
- For architecture changes, remove obsolete paths after replacement unless migration
  compatibility is explicitly required. Do not keep wrappers or dual runtimes just in case.
- Before implementation, briefly identify the Goal, minimum observable success,
  non-goals, existing reusable path, and concrete justification for each new mechanism.
- Review for deletion as well as correctness: challenge new modules/states/options,
  duplicated contracts, external responsibilities and added calls/transitions on the
  simple path. Preserve security/correctness boundaries; simplicity is not a line count.

For issue-driven work, follow [development-workflow.md](docs/development-workflow.md).
Treat the Issue's goal, design intent, acceptance criteria and non-goals as the implementation
specification; review the PR against that specification, not only against tests or local code
correctness.

See [architecture.md](docs/architecture.md) for runtime boundaries and
[development.md](docs/development.md#architecture-canary) for the architecture canary.

## Development and verification

- Follow [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/development.md](docs/development.md)
  for setup, supported Python versions, and the verification sequence. Keep those
  documents and [.github/workflows/ci.yml](.github/workflows/ci.yml) authoritative
  rather than duplicating commands here.
- For runtime changes, add focused regression tests for affected policy, state
  transitions, serialization, persistence, and replay behavior. Preserve the
  deterministic trust boundary described in [docs/security.md](docs/security.md).
- State which checks actually ran and which were skipped or blocked; passing unit
  tests does not establish Docker isolation or real-model benchmark success.

## Optional integration tests

- Docker/native isolation and live-model tests are opt-in. Follow
  [docs/isolated-worker.md](docs/isolated-worker.md) for their explicit configuration
  and supported environments. Do not enable live model access merely to run the
  ordinary test suite.
- Use disposable fixtures and explicitly delegated credentials for opted-in tests;
  never use the operator's normal history database as a test fixture.
