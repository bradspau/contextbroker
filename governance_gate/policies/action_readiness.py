"""Pure action-readiness policy. Empty list of reasons == permitted; anything
unexpected is a denial (fail closed). No model score/confidence is an input.
All type checks are exact (no subclasses) so overridden dunder methods cannot
subvert comparisons."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from fnmatch import fnmatchcase

from data_contracts.envelope import (DEFAULT_MAX_AGE_S, ContextEnvelope, Quality,
                                     StateCategory)

_EVIDENCE_STATES = (StateCategory.OBSERVED, StateCategory.CONFIGURED)
FUTURE_TOLERANCE_S = 5


@dataclass(frozen=True)
class ActionProposal:
    template_id: str
    scope: str
    loop_id: str
    context: tuple[ContextEnvelope, ...]
    expected_outcome: str


def _nonempty_str(v) -> bool:
    return type(v) is str and v != ""


def is_aware(dt) -> bool:
    return type(dt) is datetime and dt.tzinfo is not None and dt.utcoffset() is not None


def _envelope_wellformed(e) -> bool:
    if type(e) is not ContextEnvelope or type(e.state) is not StateCategory:
        return False
    q = e.quality
    if type(q) is not Quality or type(q.stale) is not bool or type(q.missing) is not bool:
        return False
    if type(q.conflicts) is not list:
        return False
    return type(e.entity_id) is str


def _fresh_evidence(e, now: datetime, max_age_s: int) -> bool:
    q = e.quality
    if e.state not in _EVIDENCE_STATES or q.stale or q.missing or q.conflicts:
        return False
    if not is_aware(e.observed_at):
        return False
    age = now - e.observed_at
    return -timedelta(seconds=FUTURE_TOLERANCE_S) <= age <= timedelta(seconds=max_age_s)


def check(proposal, template, holder, now: datetime,
          max_age_s: int = DEFAULT_MAX_AGE_S) -> list[str]:
    if type(proposal) is not ActionProposal:
        return ["proposal is not an ActionProposal"]
    if not is_aware(now):
        return ["now must be a timezone-aware datetime"]
    reasons: list[str] = []
    for name in ("template_id", "scope", "loop_id", "expected_outcome"):
        if not _nonempty_str(getattr(proposal, name)):
            reasons.append(f"invalid field: {name}")
    ctx = proposal.context
    if type(ctx) is not tuple or not all(_envelope_wellformed(e) for e in ctx):
        reasons.append("invalid field: context")
    if reasons:
        return reasons

    # A wildcard/traversal scope could dodge exact-string reservations.
    if any(c in proposal.scope for c in "*?[") or ".." in proposal.scope.split("/"):
        return ["scope contains wildcard or path-traversal characters"]

    if template is None:
        reasons.append("template not approved or unknown")
    elif (not _nonempty_str(template.scope_pattern)
          or not _nonempty_str(template.expected_outcome)):
        reasons.append("template malformed")
    else:
        if not fnmatchcase(proposal.scope, template.scope_pattern):
            reasons.append("scope does not match template scope_pattern")
        if proposal.expected_outcome != template.expected_outcome:
            reasons.append("expected outcome does not match template")

    if not ctx:
        reasons.append("empty context")
    else:
        for e in ctx:
            if e.quality.stale:
                reasons.append(f"stale context: {e.entity_id}")
            if e.quality.missing:
                reasons.append(f"missing context: {e.entity_id}")
            if e.quality.conflicts:
                reasons.append(f"conflicting context: {e.entity_id}")
        if not any(_fresh_evidence(e, now, max_age_s) for e in ctx):
            reasons.append("no fresh observed/configured evidence "
                           "(inferred-only or aged context is insufficient)")

    if not (type(holder) is str and holder == proposal.loop_id):
        reasons.append("loop does not hold reservation for scope")
    return reasons
