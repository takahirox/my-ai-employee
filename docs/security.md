# Security, permissions, sandboxing, and secrets

Fleet's v0.1 runtime is local and offline. The execution policy denies unrestricted network,
credential, process, self-modification, publish, deploy, merge, and destructive-write powers
by default. Policy composition may tighten these floors but cannot weaken them.

Run Fleet with the least filesystem and process authority needed. Treat graph, project, model,
and replay data as untrusted input. Never put API keys, tokens, credentials, private keys, or
personal data in graphs, profiles, SQLite databases intended for sharing, logs, or evidence.
Pass secrets through an operator-controlled secret facility outside Fleet and redact command
output before persistence. The Project Harness records intent; it is not a security sandbox.

v0.1 does not provide containers, remote providers, network execution, or a distributed
security boundary. Report vulnerabilities according to `SECURITY.md`.
