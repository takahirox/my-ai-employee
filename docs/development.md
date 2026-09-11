# Development

Requires Python 3.11 or newer and `uv`.

```text
uv sync --extra dev
.venv/bin/ruff format .
.venv/bin/ruff check --fix .
.venv/bin/pytest -q
.venv/bin/mypy src
.venv/bin/python -m build
```

Run `ruff format --check .`, `ruff check .`, `mypy src`, `pytest -q`, and `git diff --check`
before proposing a change. Add tests for policy, state-transition, serialization, persistence,
and replay changes. Tests must not use a network service. Do not weaken safety floors, loop
bounds, generation fences, canonical serialization, or evidence-gated completion to make a
test pass.

Build artifacts belong in `dist/` and are not committed. For a release candidate, install the
wheel into a fresh temporary environment and verify `import ai_employee` and `fleet --help`.

CI also checks out only the public smoke, execution-helper and request-construction
files from pocket-agent-bench PR #9 commit
`cc4be01f8857d7c4c70630cb5fa87ba8884a15c8`. Tests verify their SHA-256 identities,
execute the exact public smoke with normal `__file__`/import context, and pass the
upstream request expression through Fleet's run/cleanup interface. No grader,
solution, live model or network service is used by pytest. Third-party sources are
not vendored into this repository.

For the same local check, provide those pinned files under
`$FLEET_PUBLIC_CONTRACT_ROOT/src/pocket_bench/` and run the ordinary pytest command
with that environment variable set. Without the files, the two public-artifact
tests explicitly skip; the remaining hermetic runtime/native-contract tests still
run. Do not report a skipped public-artifact check as integration success.

## Architecture canary

`tests/test_autonomous_runtime.py::test_actual_worker_candidate_is_verified_published_and_replayed_without_work`
is the simple production-path canary: one file-producing Task goes through the real
Engine, Journal, immutable Candidate, independent verification and promotion. It
checks one Worker, no human/authority waits or graph extension, and no new work on
replay. Its current fixture policy disables optional reviews; the five model-boundary
calls are a test baseline, not a universal product call-count invariant.

The model boundary is a deterministic fixture that edits and checks actual files;
the runtime is not mocked and no benchmark adapter is involved. This ordinary CI
canary detects orchestration regressions, not real-model quality or Docker isolation.
`test_configured_replan_limit_prevents_new_model_attempt` in the same file exercises
a deliberately failing Candidate and verifies its durable failure evidence. Keep
native-isolation and explicitly opted-in live-model validation separate.

When the simple path gains calls, transitions or failure points, review the concrete
requirement that needs them. Do not add a special production shortcut to satisfy the
canary or remove necessary policy/verification boundaries to reduce its call count.
