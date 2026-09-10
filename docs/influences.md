# Design influences

Fleet's earlier design drew conceptual lessons from the
[OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents) and
[TAKT](https://github.com/nrslib/takt): small contracts, explicit policy,
observable execution and bounded review/recovery. This repository does not copy
those projects' source or compatibility surfaces.

The current design separates orchestration from autonomous worker internals.
Runtime-owned facts include identities, authority, budgets, Candidate bytes and
lineage. Model-backed stages reason about requirements, plans and evidence.
[architecture.md](architecture.md) describes the maintained runtime; Git preserves
the historical implementation and its design decisions.
