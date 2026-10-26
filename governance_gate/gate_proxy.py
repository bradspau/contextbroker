"""Fail-closed action-readiness gate. AI proposes; the gate authorises."""
from dataclasses import dataclass
from datetime import datetime

from data_contracts.envelope import DEFAULT_MAX_AGE_S
from governance_gate.policies.action_readiness import ActionProposal, check, is_aware

__all__ = ["ActionProposal", "Decision", "Gate"]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: tuple[str, ...]
    record_id: str


class Gate:
    def __init__(self, catalogue, reservations, records, max_age_s: int = DEFAULT_MAX_AGE_S):
        self._max_age_s = max_age_s
        self._catalogue = catalogue
        self._reservations = reservations
        self._records = records

    def evaluate(self, proposal, now: datetime) -> Decision:
        self._seen = {"template": None, "holder": None}
        try:
            reasons = self._decide(proposal, now)
        except Exception as exc:  # fail closed: never raise out of evaluate
            reasons = [f"gate error: {type(exc).__name__}"]
        seen = self._seen
        allowed = not reasons
        record_id = ""
        try:
            record_id = self._records.append("gate_decision", {
                "template_id": self._s(proposal, "template_id"),
                "scope": self._s(proposal, "scope"),
                "loop_id": self._s(proposal, "loop_id"),
                "allowed": allowed,
                "reasons": list(reasons),
                "template_version": self._s(seen["template"], "version"),
                "evidence": self._evidence(proposal),
                "reservation_holder": seen["holder"] if type(seen["holder"]) is str else None,
                "evaluated_at": self._iso(now),
                "max_age_s": self._max_age_s,
            })
        except Exception as exc:
            allowed = False
            reasons = list(reasons) + [f"record error: {type(exc).__name__}"]
        return Decision(allowed, tuple(reasons), record_id)

    @staticmethod
    def _s(proposal, name):
        v = getattr(proposal, name, None)
        return v if type(v) is str else None

    @staticmethod
    def _iso(dt):
        try:
            return dt.isoformat() if type(dt) is datetime else None
        except Exception:
            return None

    @classmethod
    def _evidence(cls, proposal):
        ctx = getattr(proposal, "context", None)
        if type(ctx) is not tuple:
            return []
        out = []
        for e in ctx:
            state = getattr(e, "state", None)
            out.append({
                "entity_id": cls._s(e, "entity_id"),
                "state": getattr(state, "value", None) if type(getattr(state, "value", None)) is str else None,
                "observed_at": cls._iso(getattr(e, "observed_at", None)),
            })
        return out

    def _decide(self, proposal, now) -> list[str]:
        if type(proposal) is not ActionProposal or not is_aware(now):
            return check(proposal, None, None, now)
        template = holder = None
        if type(proposal.template_id) is str and type(proposal.scope) is str:
            template = self._catalogue.get(proposal.template_id)
            holder = self._reservations.holder(proposal.scope, now)
            self._seen = {"template": template, "holder": holder}
        return check(proposal, template, holder, now, self._max_age_s)
