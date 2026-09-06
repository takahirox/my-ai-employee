# Dependent code workspaces

Accepted writing predecessors now seed a dependent node's isolated Git workspace
before its worker starts. A consumer can import an API produced by its predecessor,
and a serial successor can modify that API in the same file. This is materialized
code, not artifact text added to the model prompt.

The original repository commit remains the common baseline. Each dependent attempt
persists `workspace_input_v2` with the exact worker request, graph revision,
generation, attempt, accepted predecessor result/patch bindings and input Git tree.
`workspace_output_v2` binds that input to the cumulative accepted patch, output tree
and an incremental patch relative to the input tree. Inspector's graph-run projection
exposes these records under `workspace_lineage`; it does not return patch bodies.

Final composition applies roots' patches and dependent nodes' increments in graph
order. It reconstructs input/output trees and checks exact accepted ancestry before
allowing serial overlap. Cumulative patches remain the authoritative node artifacts,
so existing acceptance, verification and promotion bindings keep their meaning.
Changed upstream result identity invalidates downstream composition even when the
patch bytes happen to match. Reopening records and replay do not execute workers,
apply patches or promote changes.

Materialization uses the existing exact-path edit and policy boundary. Source
workspaces must still match accepted artifacts and the original baseline. Generated
file exclusions are unchanged. Mandatory node and integrated parent verification,
semantic review when configured, and explicit promotion approval remain required.
The original checkout is never modified by materialization or composition.

Supported initial scope: serial writing dependencies (including same-file edits),
transitive ancestry, inherited code through non-writing nodes, and disjoint
independent frontiers. Overlapping independent branches require explicit integration
and fail closed; this is not a general merge/conflict-resolution engine. Replans
retaining writing outputs from a different accepted graph revision are conservatively
rejected rather than silently rebinding lineage. Pause/resume may retain older-generation
outputs under the same accepted revision, but only through the exact scheduler-authorized
predecessor references (including their original result generation and attempt).
A writing node must produce an applicable nonempty
increment; use a non-writing node for a pure observation/verification step.

Regression coverage uses real disposable Git workspaces and deterministic adapters:
API reuse, serial same-file composition, disjoint fork/join, stale upstream identity,
tampered output lineage, source mutation, policy denial, persistence, replay and
unchanged integrated-verification/promotion gates. These tests do not establish live
model quality or Docker-native integration success.
