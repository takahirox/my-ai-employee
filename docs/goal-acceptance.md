# Request-specific acceptance (#83)

Shared Harness tests are necessary but need not prove the requested change. The
first milestone adds explicitly operator-authored acceptance checks before dispatch:

```json
{
  "schema_version": "1",
  "goal": "change c.txt",
  "criteria": [{
    "id": "requested-content",
    "request_fragment": "c.txt",
    "description": "c.txt contains c-after followed by a newline",
    "command_ref": "goal-check"
  }]
}
```

Declare `goal-check` in `.fleet/project.json` commands before accepting the Goal:

```json
{"argv": ["python", "-I", "-c", "from pathlib import Path; assert Path('c.txt').read_text() == 'c-after\\n'"]}
```

Run `fleet work 'change c.txt' --acceptance-file goal-checks.json` with the usual
operator/routing options. The input's Goal must match exactly, and each criterion
must cite an exact nonblank original-request fragment. No additional model call is
required. Criteria enter the persisted Goal under `goal.acceptance.*`; commands and
evaluators become mandatory alongside existing Harness checks.

The first milestone deliberately accepts only declared `python -I -c` checks without
inherited environment, not candidate-writable test scripts. It does not automatically
invent the correct semantic test for arbitrary requests. A request fragment ties the
check to intent but is not proof of semantic adequacy: the operator still chooses and
reviews the check. Subjective or otherwise unsupported criteria must not be described
as mechanically proved.

Persisted Goal and exact Harness digest govern resume and promotion. Changing/deleting
the original input file cannot relax them; changing the Harness check invalidates old
evidence. Candidate freshness, independent verification, approval and explicit promotion
remain enforced. Tests include shared-check false positives, corrected candidates,
foreign Goals, unknown commands, weakened criteria and changed checks at promotion.
