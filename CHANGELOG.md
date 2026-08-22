# Changelog

## 0.2.1

- Applied the repository formatter to the v0.2 implementation so the full CI
  quality gate passes. Runtime behavior and the v0.2 feature set are unchanged.

## 0.2.0

- Added strict Project Harness v2 discovery, migration, policy precedence, and
  default-deny network/install authority.
- Added controlled process execution, bounded artifacts, restricted HTTPS downloads,
  project-local installation, digest-bound approvals, and isolated Git workspaces.
- Added Codex and Claude Code proposal adapters. Model output is validated as a typed
  envelope; declared unified diffs are applied by the deterministic workspace service.
- Added durable work runs, checkpoint/resume controls, verification evidence,
  protected-path review, exact-digest promotion, and v0.2 Inspector projections.
- Kept v0.1 graph execution, replay, CLI, and SQLite readers compatible.

## 0.1.0

- Initial Trust Kernel with typed domain state, graph acceptance, bounded runtime,
  evidence-gated completion, context compilation, Project Harness, routing, SQLite
  persistence, replay, CLI, and read-only Inspector.
