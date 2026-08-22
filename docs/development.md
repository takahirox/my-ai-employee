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
