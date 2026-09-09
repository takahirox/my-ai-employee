# Model-stage contracts

A model response is a proposal. Structural validity, request-specific reference validity,
process correlation, and semantic correctness are separate checks.

## One request, one reference contract

Parent, task, and plan reviewers derive a `ReferenceContract` from their accepted inputs.
The same reference sets supply the prompt, constrained output schema, and acceptance
checks. Parent coverage uses Goal criteria; task coverage uses that task's criteria.
Finding references are subsets, not additional criteria. Plan-wide findings may have no
node references. New plan nodes and finding IDs are generated labels, not references to
be restricted to existing IDs.

Schemas constrain permitted values and cardinality where supported. The deterministic
validator still enforces exact coverage, uniqueness, bindings, and evidence provenance.
Provider schema enforcement is not proof of acceptance. Existing transport ordering
normalization does not remove duplicates or replace unknown references.

Codex gets an invocation-private temporary schema file, retained through process execution
and removed afterward. Claude gets the same schema via `--json-schema`. Ollama receives
it in the prompt with JSON mode; its CLI does not enforce that schema. These differences
are tested with fake executors capturing the actual invocation, not claimed as live-model
verification. The initial and revision planners use the same file-lifetime mechanism for
their already bounded graph schemas.

## Correlation before output

Model process results must match both the originating run ID and process request digest
before output is read or accepted. Shared helpers enforce this at planning, review,
assessment and worker boundaries. Initial planning also validates the exact policy decision
before execution. Task review checks parent/child run, attempt, Harness and policy bindings
before exposing the child snapshots to its reviewer.

## Fail at the responsible stage

A malformed response, unknown reference or foreign process result is not a finding that
an implementation is defective. Task-review protocol failures have bounded reason codes;
plan-review reference failures are classified separately from stale process bindings.
Parent review unavailability cannot schedule a child code repair. Existing bounded repairs
for actual semantic findings and failed candidate verification remain available.

This change introduces no automatic model retry, model switch, new allowance, or deadline
extension. Candidate and diagnostic records are retained. It does not promise that a timed
out review or previously rejected candidate would pass with more time.

## Persistence and verification

Reference contracts and per-call schemas are derived transport data. Existing persisted
request/result fields and digest formats are unchanged. Read-only replay continues to use
saved evidence; it must not regenerate prompts, rerun the model, or retroactively apply new
schema constraints to historical output. A new execution gets its own contract and schema.

Regression coverage includes extra/missing/duplicate/foreign references, multiple valid
criteria, order normalization, actual per-backend schema transport, overlapping schema file
lifetimes, foreign process results before stdout, foreign policy decisions before execution,
parent-child invocation checks, and unavailable-review versus valid-code-repair transitions.

The audit covered Goal/node assessment, initial/revision planning, plan review, worker,
task review and parent review. It does not establish all backend implementations or model
responses are defect-free. The ordinary suite and optional isolation/live tests have distinct
claims; report what actually ran.

## Design references

[Pydantic AI output processing](https://github.com/pydantic/pydantic-ai/blob/main/pydantic_ai_slim/pydantic_ai/_output.py)
illustrates deriving schemas and validators from output types. Request-specific allowed
values still require explicit propagation; validation context is not automatically model
context. [LangGraph subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
illustrate explicit transformations between parent and child contracts. These are design
references, not dependencies. Task-level retries such as those described by
[CrewAI](https://docs.crewai.com/v1.15.20/en/concepts/tasks) are not copied into review recovery:
a failed review response must not repeat completed implementation or external operations.
