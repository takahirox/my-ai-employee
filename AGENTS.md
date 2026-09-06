# My AI Employee repository guidance

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
