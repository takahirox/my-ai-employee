# Development workflow

This document is the authoritative workflow for issue-driven implementation and review.
Agent-specific entry files such as `AGENTS.md` and `CLAUDE.md` should point here rather than
copying this process.

## Principle

A pull request is complete only when it implements the **intent of the Issue**, not merely when
its code is locally correct or its tests pass.

For every non-trivial change, preserve this traceability:

```text
Issue goal / design intent / acceptance criterion
    -> implementation location
    -> test or other evidence
    -> review conclusion
```

Do not allow a PR to silently narrow a broad architectural requirement into only the failure case
that first exposed it.

## 1. Before implementation: read the Issue as the specification

Before implementation or review, also check for later changes explicitly agreed with the user in
conversation or elsewhere. Those decisions supersede outdated Issue text; a suggestion or a newer
comment alone does not establish agreement. Record the agreed change in the Issue body or a clear
decision comment, identifying what it supersedes, so reviewers can follow the current specification
without access to the original conversation. Do not request approval again for an already agreed
change.

Read the full Issue, including these recorded decisions and later comments. Identify:

- the user-visible or runtime outcome that must change;
- the root problem the Issue is trying to prevent, not only the observed symptom;
- architectural principles and responsibility boundaries that must remain true;
- every acceptance criterion;
- explicit non-goals;
- existing code paths and authorities that should be reused rather than duplicated.

Before coding, summarize the intended change in a small implementation plan. For every new
abstraction, state, retry, adapter or subsystem, identify the concrete current requirement,
observed failure, security/correctness invariant or unavoidable external contract that requires it.

## 2. Implement the smallest change that satisfies the whole Issue

Prefer the simplest existing path that can satisfy the specification. Do not generalize for a
hypothetical future requirement.

When an Issue establishes an authoritative contract or source of truth, apply it end-to-end.
Do not implement only one projection of it. For example, if downstream runtime validation rejects
a value for semantic reasons, review whether generation, provider schema, prompt-visible context,
review and repair receive the same relevant semantics from the same authority.

A useful reverse check is:

```text
What can the runtime reject?
    -> could the producer know that rule before generating the value?
    -> is that rule mechanically projected into generation where possible?
    -> does repair receive the same rule when the output is fixable?
```

Strict acceptance with an underspecified producer contract is a design defect, even when the
accepted data is structurally typed.

## 3. PR description: provide Issue-to-code traceability

The PR description must summarize the Issue intent and include a compact traceability section for
material requirements. Use this shape when useful:

| Issue requirement / intent | Implementation | Evidence |
| --- | --- | --- |
| What must be true | File/function/design choice | Test, invariant, inspection or experiment |

Also state:

- important non-goals that remain intentionally unimplemented;
- any acceptance criterion not yet satisfied;
- validation actually performed and anything skipped;
- known limits of the evidence (for example, hermetic tests do not prove live-model quality).

Do not claim an Issue is closed merely because a narrower regression test passes.

## 4. Review in two passes

### Intent and architecture review

Read the Issue before reviewing the patch. Ask:

- Does the PR solve the root problem the Issue describes, rather than only the triggering example?
- Are all design principles and responsibility boundaries in the Issue reflected in the code?
- Can every acceptance criterion be mapped to implementation and evidence?
- Has any Issue requirement been weakened, omitted or reinterpreted without an explicit decision?
- If the Issue requires one authoritative contract/source of truth, are all relevant projections
  actually derived from it rather than hand-maintained in parallel?
- For each deterministic downstream rejection rule, does the upstream producer receive the
  semantic information needed to avoid or repair a fixable invalid proposal?
- Has the implementation introduced complexity outside the Issue's actual needs?

### Implementation and correctness review

Then review the patch normally for:

- correctness and failure handling;
- security and authority boundaries;
- persistence/resume/replay behavior;
- concurrency and budget/accounting behavior where relevant;
- regression coverage and negative tests;
- deletion of obsolete code and duplicated contracts;
- bounded/redacted diagnostics rather than indiscriminate raw logging.

Passing this second pass does not compensate for failing the first.

## 5. Pre-merge Issue re-read

Before considering the PR ready to merge, re-read the Issue from the beginning and answer:

> Does this PR eliminate the reason this Issue had to be created, while preserving its stated
> architecture and non-goals?

Then explicitly look for important Issue intent that is not represented in the patch or tests.
If such a gap exists, either fix it in the PR or record an explicit scope decision before merge;
do not silently close the Issue around the partial implementation.

## 6. Validation strategy

Use the repository verification sequence in `docs/development.md` and add focused tests for the
changed behavior. Tests should validate the contract at the boundary where failures historically
occurred, not only isolated helper functions.

For contract changes, prefer end-to-end projection tests that cover the relevant chain, such as:

```text
authoritative definition
    -> generated/provider-visible contract
    -> model/fixture output
    -> deterministic validation
    -> repair/review when applicable
    -> accepted downstream state
```

Use live-model or external integration tests only when their additional evidence is necessary and
explicitly enabled. Do not make ordinary correctness depend on a benchmark-specific product path.

## 7. Review output

A review should distinguish:

- **blocking gaps**: the PR does not fulfill Issue intent, an acceptance criterion, or a required
  correctness/security invariant;
- **non-blocking follow-ups**: useful improvements that are not required by the current Issue;
- **evidence limits**: claims the performed validation cannot establish.

Avoid turning speculative follow-ups into implementation requirements in the current PR. This
keeps the review faithful to both the Issue and the simplicity-first development principle.
