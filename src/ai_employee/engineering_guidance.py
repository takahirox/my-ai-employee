"""Shared engineering guidance; never execution or acceptance authority."""

SIMPLICITY_GUIDANCE = (
    "Simplicity is a positive engineering objective. Among approaches satisfying the same "
    "current requirements with comparable correctness, safety and maintainability, prefer "
    "fewer concepts, layers, abstractions, dependencies, configuration, states and moving "
    "parts. Justify added complexity with a concrete current requirement or observed evidence, "
    "not hypothetical future usefulness. Reuse an existing mechanism when suitable; do not "
    "force reuse that makes the solution more complex. This is not a smallest-diff rule or "
    "permission for unrelated cleanup. Never remove required verification, error handling, "
    "compatibility, performance, security, policy or approval controls for simplicity. Stop "
    "when the accepted goal and required quality are satisfied. "
)

COMMENT_GUIDANCE = (
    "Prefer self-explanatory code. Do not add inline comments by default; use them for "
    "non-obvious WHY: rationale, constraints, invariants, trade-offs or surprising external "
    "behavior, not narration of obvious WHAT/HOW. Preserve useful existing comments and "
    "required public API, schema and protocol documentation, including behavior contracts. "
    "Remove stale or redundant comments only within the current change when safe; do not "
    "refactor unrelated code just to eliminate comments. A necessary TODO must name its "
    "blocking reason or removal condition. Project documentation and style requirements "
    "remain authoritative. "
)

SIMPLICITY_REVIEW_GUIDANCE = SIMPLICITY_GUIDANCE + (
    "Identify unjustified structural complexity even within scope, using supplied evidence. "
    "Ask which present requirement justifies each added mechanism and whether a simpler "
    "design preserves the same quality. Do not reject justified complexity or mistake "
    "under-engineering for improvement. Missing artifact bodies are not evidence of a defect."
)

COMMENT_REVIEW_GUIDANCE = COMMENT_GUIDANCE + (
    "Flag redundant narration only when supported by the supplied code or evidence. "
    "Do not block a correct task for comment-style preference unless an accepted project "
    "quality requirement makes it mandatory; otherwise keep it advisory."
)
