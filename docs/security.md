# Security boundaries

Fleet enforces environment and runtime boundaries, not the correctness of every
internal worker thought or SDK action. LLM assessments remain fallible; operator
checks and independent review provide evidence rather than a universal correctness
proof.

## Execution and credentials

Production execution requires an explicitly prepared immutable Docker image. The
container has no host bind mounts, host history, host Git metadata, Docker socket,
or unrelated credentials. Its root filesystem is read-only; CPU, memory, process
count and writable storage are bounded. The existing seccomp process admission
supervisor kills and reaps task-UID processes before accepting a snapshot. Container
lifetime also bounds an invocation after controller failure.

Codex's native permission profile allows minimal runtime reads and task workspace
writes. Runtime metadata is denied and integration inputs are read-only. A canary
outside the permitted native workspace must remain unreadable. Native startup
failure blocks the stage; there is no unrestricted host fallback. macOS native
sandboxing alone is insufficient for Fleet's lifetime guarantee: testing showed a
detached child could survive its parent. The outer namespace closes that gap.

Model authentication must be an explicitly delegated file. It enters the isolated
model environment, outside native tool read scope. The adapter does not search the
operator's normal authentication locations. Use disposable delegated credentials
for live tests; ordinary tests do not call models or inject credentials.

## Authority

The immutable Run policy defines the authority ceiling. Task proposals and dynamic
grants are checked against it. Strict policy requires real operation-level controls
for external writes; unsupported controls block the Task. Balanced/permissive policy
can explicitly allow a broader HTTPS host grant and records that decision.

The gateway allows configured destinations and logs at most 1,000 bounded
host/decision records per environment. It does not log bodies, credentials, URL
queries or arbitrary rejected destination text. HTTPS contents remain opaque.
A coarse write-capable grant does not imply approval of every operation.

Service credential injection, enforceable read-only remote access, mandatory
pre-operation approval and strict duplicate prevention are not implemented by a
universal action proxy. Requests needing an unavailable boundary block. Unknown
external results do not cause another Fleet-driven attempt. External acceptance
requires an operator-owned evidence check; current generic verification cannot
query remote state under an enforceable read-only grant. SDK-internal retries are
outside Fleet's exactly-once guarantees.

Authority requests persist a durable wait and release the live worker invocation.
Approval is recorded before application; APPLIED follows native confirmation.
Continuation reconciles owned environments first. Revocation stops the Run and is
confirmed only after cleanup. Unconfirmed resource creation or cleanup blocks
continuation instead of assuming that old authority disappeared.

## Results, history and budgets

Snapshots accept bounded regular files, reject symlinks/special files, and validate
content hashes when read or materialized. Reserved runtime metadata is excluded.
Verification-side changes never become Candidate bytes. Publication requires a new
destination and rechecks identity, lineage and terminal authority state.

The private journal also retains bounded proposal/review/check diagnostic text,
including rejected responses. Credential-pattern redaction is best effort; ordinary
task content remains sensitive. See [retention rules](run-interface.md#diagnostic-retention).
These records carry no execution or acceptance authority and do not reset budgets.

Workers cannot access the journal or Candidate store. Digest chaining detects
accidental or partial history changes; it is not cryptographic authentication
against a malicious operator who can rewrite both data and hashes.

Wall time, invocation time, task/attempt counts and graph growth provide stopping
boundaries. Token/cost reservations control admission across concurrent calls.
When backend accounting is absent, the reservation remains charged and usage is
marked unknown. A reservation is not a provider-enforced per-call token/cost cap:
actual spend can exceed the estimate before usage arrives. Exhaustion stops new
work and cancels active invocations; it never triggers purchases or quota resets.

The Inspector binds to loopback, checks Host and bearer authorization, exposes no
mutation endpoint, and uses text rendering for untrusted content. Its token is
carried in the URL fragment rather than an HTTP query.
