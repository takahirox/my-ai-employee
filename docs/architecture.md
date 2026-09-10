# Architecture

Fleet owns the Goal, Plan, scheduling, environment grants, budgets, immutable
Candidates, verification and history. The native worker owns its internal work.
There is no action proposal vocabulary and no replay of implementation edits.

`models.py` defines frozen, strict contracts. Original Input remains unchanged.
Clarification records success criteria, exact requirement fragments, assumptions
and unresolved questions. Configured review compares that specification against the
original; unresolved clarification cannot reach planning. Mandatory check IDs refer
only to operator-owned commands in the snapshotted Run configuration.

`engine.py` schedules one Planner's DAG. Ready independent tasks run concurrently;
integration is an ordinary task and can occur at multiple convergence points.
Multiple upstream snapshots are available under `.fleet-inputs/<index>`, read-only
inside the native sandbox. A successful task freezes its actual workspace in
`candidates.py`. Every Candidate binds its task definition, attempt, upstream
Candidate digests and authority version.

Verification uses fresh copies of those exact bytes. Operator checks each get a
separate copy; verifier review also starts from the original Candidate rather than
a verifier-modified workspace. Task acceptance does not imply Goal acceptance.
Publication checks the adopted graph and exact lineage again, then materializes the
verified snapshot to a fresh destination. It does not execute worker edits again.

Recovery preserves a task's workspace where safe, supports configured worker/model/
effort escalation, and can extend the graph with new task identities. Historical
Task definitions cannot be rewritten. Repaired upstream results require new
downstream work; old downstream Candidates keep their original bindings. The Run
budget and graph-growth limits apply to every extension.

`history.py` owns an append-only, digest-checked SQLite event journal and transactional
reservations. A controller lock prevents simultaneous controllers for one Run.
Resource leases coordinate overlapping coarse external write grants across Runs;
unknown outcomes do not release those leases by inference. Approval REQUESTED,
APPROVED and APPLIED events are distinct. Owned container ledgers support cleanup
before continuation and revocation.

`container.py` reuses the existing Docker namespace and native process guard.
Workers execute in the native Codex sandbox inside that namespace. The guard stops
and reaps task descendants before workspace capture, including detached processes.
The model gateway carries model traffic and permitted HTTPS destinations while
recording bounded metadata. It does not interpret arbitrary encrypted operations.

`cli.py`, `inspector.py`, and `benchmark.py` consume these same runtime facts. There
is no legacy runtime, compatibility profile router or database migration subsystem.
