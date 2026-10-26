"""Pure action-readiness policy. Empty list of reasons == permitted; anything
unexpected is a denial (fail closed). No model score/confidence is an input."""
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase

from data_contracts.envelope import ContextEnvelope, StateCategory

_EVIDENCE_STATES = (StateCategory.OBSERVED, StateCategory.CONFIGURED)


@dataclass(frozen=True)
class ActionProposal:
    template_id: str
    scope: str
    loop_id: str
    context: tuple[ContextEnvelope, ...]
    expected_outcome: str


def _nonempty_str(v) -> bool:
    return isinstance(v, str) and v != ""


def check(proposal, template, holder, now: datetime) -> list[str]:
    if not isinstance(proposal, ActionProposal):
        return ["proposal is not an ActionProposal"]
    reasons: list[str] = []
    for name in ("template_id", "scope", "loop_id", "expected_outcome"):
        if not _nonempty_str(getattr(proposal, name)):
            reasons.append(f"invalid field: {name}")
    ctx = proposal.context
    ctx_ok = isinstance(ctx, tuple) and all(isinstance(e, ContextEnvelope) for e in ctx)
    if not ctx_ok:
        reasons.append("invalid field: context")
    if reasons:
        return reasons

    if template is None:
        reasons.append("template not approved or unknown")
    else:
        if not fnmatchcase(proposal.scope, template.scope_pattern):
            reasons.append("scope does not match template scope_pattern")
        if proposal.expected_outcome != template.expected_outcome:
            reasons.append("expected outcome does not match template")

    if not ctx:
        reasons.append("empty context")
    else:
        usable = 0
        flagged = False
        for e in ctx:
            flagged = flagged or e.quality.stale or e.quality.missing or bool(e.quality.conflicts)
            if e.quality.stale:
                reasons.append(f"stale context: {e.entity_id}")
            if e.quality.missing:
                reasons.append(f"missing context: {e.entity_id}")
            if e.quality.conflicts:
                reasons.append(f"conflicting context: {e.entity_id}")
            if (e.state in _EVIDENCE_STATES and not e.quality.stale
                    and not e.quality.missing and not e.quality.conflicts):
                usable += 1
        if usable == 0 and not flagged:
            reasons.append("inferred or non-evidential context alone is insufficient")

    if holder != proposal.loop_id:
        reasons.append("loop does not hold reservation for scope")
    return reasons

