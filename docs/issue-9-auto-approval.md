# Policy-controlled promotion approval

Human promotion approval remains the default. The first policy milestone adds one conservative,
deterministic rule; it does not add `always`, a policy DSL, or automatic promotion.

Policy approval requires two independent, versioned opt-ins:

1. the operator configuration sets `promotion_auto_approval.mode: policy`, lists the repository's
   absolute path, and supplies file/risk/patch limits; and
2. the repository Project Harness sets `approvals.promotion: policy`.

If either opt-in or any exact evidence is missing, stale, duplicated, foreign, or ambiguous, Fleet
persists or creates a normal pending manual approval. A provisional Harness can never opt in.

```yaml
# operator config
promotion_auto_approval:
  mode: policy
  allowed_repositories: [/absolute/path/to/project]
  max_risk: 0
  max_changed_files: 5
  max_patch_bytes: 100000
```

```yaml
# .fleet/project.yaml
approvals:
  promotion: policy
```

The rule applies only to a graph parent candidate after exact parent evaluation PASS. It requires
at least one configured deterministic evaluator, fresh PASS ledgers, complete Goal acceptance, and
a clean PASS semantic review when semantic review is enabled. Network/download/install capability,
protected paths, `.fleet/**`, `.github/**`, dependency manifests and lockfiles, excessive risk,
or configured size limits always require manual approval.

Policy approval still creates an approved `ApprovalRecord`. It is bound to a durable
`PromotionPolicyDecision` containing the exact candidate, accepted graph revision and generation,
composition, Harness, effective policy, operator configuration, parent/Goal/evaluation/semantic
evidence, repository, rule configuration, node risk/capability facts, paths, file count, and patch
size. `fleet inspect`, `fleet replay`, and `fleet explain` expose the source, rule, reason, and
body-free bindings without running a Worker or evaluator.

`fleet promote` remains an explicit operation. Immediately before changing the repository it
reloads the operator configuration and Harness, revalidates the exact parent evidence, and
recomputes the deterministic rule facts. Any mismatch returns `STALE_PROMOTION_APPROVAL` without
changing the repository.

