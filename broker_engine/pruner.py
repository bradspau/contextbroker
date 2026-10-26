"""Bound the context returned to a consumer without hiding quality problems."""
from dataclasses import dataclass, field

from data_contracts.envelope import ContextEnvelope


@dataclass
class PrunedContext:
    kept: list[ContextEnvelope]
    dropped_count: int
    reasons: list[str] = field(default_factory=list)


def _is_healthy(env: ContextEnvelope) -> bool:
    q = env.quality
    return not (q.stale or q.missing or q.conflicts)


def prune(envelopes: list[ContextEnvelope], max_entities: int) -> PrunedContext:
    """Keep at most max_entities envelopes; stale/missing/conflicting ones are
    kept in preference to healthy ones. Original order is preserved."""
    if max_entities < 0:
        raise ValueError("max_entities must be >= 0")
    if len(envelopes) <= max_entities:
        return PrunedContext(kept=list(envelopes), dropped_count=0, reasons=[])
    ranked = sorted(range(len(envelopes)), key=lambda i: (_is_healthy(envelopes[i]), i))
    keep_idx = set(ranked[:max_entities])
    kept = [e for i, e in enumerate(envelopes) if i in keep_idx]
    dropped = [e for i, e in enumerate(envelopes) if i not in keep_idx]
    reasons = []
    healthy_dropped = sum(1 for e in dropped if _is_healthy(e))
    if healthy_dropped:
        reasons.append(f"dropped {healthy_dropped} healthy envelopes")
    unhealthy_dropped = len(dropped) - healthy_dropped
    if unhealthy_dropped:
        reasons.append(f"dropped {unhealthy_dropped} unhealthy envelopes (max_entities exceeded)")
    return PrunedContext(kept=kept, dropped_count=len(dropped), reasons=reasons)
