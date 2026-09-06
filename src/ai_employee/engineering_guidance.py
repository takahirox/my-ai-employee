"""Shared engineering guidance; never execution or acceptance authority."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

_MINIMAL_SUFFICIENT = ContextVar("minimal_sufficient_guidance", default=True)


@contextmanager
def guidance_scope(enabled: bool) -> Iterator[None]:
    token = _MINIMAL_SUFFICIENT.set(enabled)
    try:
        yield
    finally:
        _MINIMAL_SUFFICIENT.reset(token)


def configured_instruction(value: str) -> str:
    """Ablate preferences only, never Goal data, acceptance or mandatory scope guards."""
    if _MINIMAL_SUFFICIENT.get():
        return value
    return (
        value.replace(SIMPLICITY_GUIDANCE, "")
        .replace(
            "shortest bounded dependency DAG sufficient for the entire accepted Goal; "
            "minimal_sufficient is the default.",
            "bounded dependency DAG sufficient for the entire accepted Goal.",
        )
        .replace(
            "Use minimal_sufficient as the default: propose the smallest change "
            "sufficient for the supplied node goal and accepted plan, prefer existing "
            "mechanisms, stay within both, and omit optional follow-on work.",
            "Propose changes satisfying the supplied node goal and accepted plan within "
            "their accepted scope.",
        )
        .replace(
            "Do not add speculative framework, abstraction, extension point, "
            "optimization, cleanup, or unrelated refactor work. ",
            "",
        )
    )


def configured_rubric(rubric: Mapping[str, object]) -> dict[str, object]:
    result = dict(rubric)
    rules = result.get("rules")
    if not _MINIMAL_SUFFICIENT.get() and isinstance(rules, tuple):
        result["rules"] = tuple(rule for rule in rules if rule != SIMPLICITY_REVIEW_GUIDANCE)
    return result


INVESTIGATION_GUIDANCE = (
    "Before committing to implementation details, inspect relevant repository code and "
    "existing tests with authorized tools within the current task's time, process and scope "
    "bounds. Relate observed facts to the accepted criteria, then implement and verify. "
    "Do not infer low risk from a small task or missing information. Preserve any required "
    "comprehensive investigation. If findings require new scope, authority, missing criteria "
    "or consequential design decisions, stop with concrete findings for supported escalation "
    "or replanning; do not silently widen the task, revise acceptance, or switch models. "
)

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
