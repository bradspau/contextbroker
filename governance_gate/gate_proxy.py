"""Fail-closed action-readiness gate. AI proposes; the gate authorises."""
from dataclasses import dataclass
from datetime import datetime

from governance_gate.policies.action_readiness import ActionProposal, check

__all__ = ["ActionProposal", "Decision", "Gate"]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: tuple[str, ...]
    record_id: str


class Gate:
    def __init__(self, catalogue, reservations, records):
        self._catalogue = catalogue
        self._reservations = reservations
        self._records = records

    def evaluate(self, proposal, now: datetime) -> Decision:
        try:
            reasons = self._decide(proposal, now)
        except Exception as exc:  # fail closed: never raise out of evaluate
            reasons = [f"gate error: {type(exc).__name__}"]
        allowed = not reasons
        record_id = ""
        try:
            record_id = self._records.append("gate_decision", {
                "template_id": self._s(proposal, "template_id"),
                "scope": self._s(proposal, "scope"),
                "loop_id": self._s(proposal, "loop_id"),
                "allowed": allowed,
                "reasons": list(reasons),
            })
        except Exception as exc:
            allowed = False
            reasons = list(reasons) + [f"record error: {type(exc).__name__}"]
        return Decision(allowed, tuple(reasons), record_id)

    @staticmethod
    def _s(proposal, name):
        v = getattr(proposal, name, None)
        return v if isinstance(v, str) else None

    def _decide(self, proposal, now) -> list[str]:
        if not isinstance(proposal, ActionProposal):
            return check(proposal, None, None, now)
        template = holder = None
        if isinstance(proposal.template_id, str) and isinstance(proposal.scope, str):
            template = self._catalogue.get(proposal.template_id)
            holder = self._reservations.holder(proposal.scope, now)
        return check(proposal, template, holder, now)
