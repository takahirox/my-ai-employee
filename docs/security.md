# Security, permissions, sandboxing, and secrets

Fleet is local-first. The execution policy denies unrestricted network, credential,
self-modification, publish, deploy, merge, and destructive-write powers by default.
Policy composition may tighten these floors but cannot weaken them.

Run Fleet with the least filesystem and process authority needed. Treat graph, project, model,
and replay data as untrusted input. Never put API keys, tokens, credentials, private keys, or
personal data in graphs, profiles, SQLite databases intended for sharing, logs, or evidence.
Pass secrets through an operator-controlled secret facility outside Fleet and redact command
output before persistence. The Project Harness records intent; it is not a security sandbox.

v0.2 uses a sibling Git worktree for isolation; this is not a container or hostile-code
security boundary. Model-authored output must validate as a typed proposal. General
commands pass through `LocalProcessExecutor`; model-authored source changes pass through
the workspace edit service, which requires an exact declared path set, a valid unified
diff, a matching policy digest, and an allowed writable path. Git worktree creation,
diff capture, patch preflight, and promotion are deterministic internal Git operations
encapsulated by `GitWorkspaceManager`.

HTTPS retrieval rejects non-HTTPS URLs, redirects outside the allowlist, IP literals,
private/reserved destinations, oversized bodies, and digest mismatches. Installation is
limited to declared project-local Python/Node targets; host-global installation is denied.
Neither capability is enabled by provisional inference.

CLI worker authentication remains owned by the official Codex/Claude tool. Fleet does
not inspect auth files and filters credential-like environment variables from controlled
commands. Artifact bodies are not returned by Inspector projections.

Machine-local worker executable overrides belong in Operator Config, not the repository's
Project Harness. Overrides must be absolute paths, are resolved through the controlled
`ProcessExecutor`, and do not grant additional process, filesystem, network, or project
authority. Runtime dependency directories must be explicitly listed as `path_entries`;
the unrestricted host `PATH` is never inherited.

v0.2 does not provide containers, remote execution, deployment, automatic Git commit/push,
or a distributed security boundary. Report vulnerabilities according to `SECURITY.md`.
