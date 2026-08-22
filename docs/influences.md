# Design influences and distinctions

## OpenAI Agents SDK

The official [OpenAI Agents SDK documentation](https://developers.openai.com/api/docs/guides/agents)
describes a code-first SDK with agent definitions, model/provider selection, an agent run loop,
orchestration and handoffs, guardrails and human review, resumable results/state, tools and MCP,
tracing, and evaluation. The Trust Kernel adopts the lessons that worker contracts should be
small, runs should be observable, and state and validation should be explicit.

It intentionally does **not** embed or clone that SDK. In this project an Agent is only one node
kind; generalized graph edges replace agent-specific handoffs, canonical Events replace a
vendor tracing contract, and completion remains a deterministic decision over accepted evidence
rather than an Agent's final response. v0.1 is provider-independent and offline, so it does not
claim provider-backed Agent execution, MCP integration, or streaming.

## TAKT

[TAKT](https://github.com/nrslib/takt) demonstrates the value of workflow-owned transitions,
step-specific roles and context, output contracts, bounded review/fix loops, explicit
permissions, isolated worktrees, and traceable run artifacts. Fleet adopts those control-plane
principles without copying TAKT's implementation or YAML surface.

The principal difference is intentional: TAKT is workflow-first, while Fleet is goal-driven and
graph-first. Probabilistic planners may propose or revise a Fleet graph, but validation must
accept an immutable graph revision before the deterministic runtime can execute it. This allows
progressive planning while preserving state-machine authority, policy floors, generation fences,
structured failures, SQLite persistence, and no-worker replay.

Both referenced projects have their own licenses. This repository contains original code and
uses conceptual design lessons only; it does not copy their source.
