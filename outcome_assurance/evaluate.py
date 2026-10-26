"""Deterministic three-valued outcome evaluation of an intent against observations."""
import math
import operator
from dataclasses import dataclass, field
from enum import Enum

from data_contracts.envelope import ContextEnvelope

OPERATORS = {
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    ">=": operator.ge,
    ">": operator.gt,
}


class Outcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    INCONCLUSIVE = "inconclusive"


@dataclass
class Verdict:
    outcome: Outcome
    per_interface: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and not math.isnan(x)


def _eval_one(op, target, env: ContextEnvelope | None) -> tuple[Outcome, object, str | None]:
    observed = env.value if env is not None else None
    fn = OPERATORS.get(op)
    if fn is None:
        return Outcome.INCONCLUSIVE, observed, f"unknown operator {op!r}"
    if env is None or env.quality.missing or env.value is None:
        return Outcome.INCONCLUSIVE, None, "missing observation"
    if env.quality.stale:
        return Outcome.INCONCLUSIVE, observed, "stale observation"
    if env.quality.conflicts:
        return (Outcome.INCONCLUSIVE, observed,
                f"unresolved conflicts: {'; '.join(env.quality.conflicts)}")
    if not _is_number(observed) or not _is_number(target):
        return Outcome.INCONCLUSIVE, observed, f"not comparable: {observed!r} vs {target!r}"
    return (Outcome.SUCCESS if fn(observed, target) else Outcome.FAILURE), observed, None


def evaluate_intent(intent: dict, observations: dict[tuple[str, str], ContextEnvelope]) -> Verdict:
    interfaces = intent.get("interfaces") or []
    if not interfaces:
        return Verdict(Outcome.INCONCLUSIVE, [], ["no interfaces in intent"])
    if not observations:
        return Verdict(Outcome.INCONCLUSIVE, [], ["no observations"])

    op, target = intent.get("operator"), intent.get("target_value")
    per, reasons = [], []
    for i in interfaces:
        key = (i["device"], i["interface_name"])
        outcome, observed, reason = _eval_one(op, target, observations.get(key))
        if reason:
            reason = f"{key[0]}/{key[1]}: {reason}"
            reasons.append(reason)
        per.append({"device": key[0], "interface_name": key[1],
                    "outcome": outcome.value, "observed": observed, "reason": reason})

    outcomes = {p["outcome"] for p in per}
    if Outcome.FAILURE.value in outcomes:
        overall = Outcome.FAILURE
    elif Outcome.INCONCLUSIVE.value in outcomes:
        overall = Outcome.INCONCLUSIVE
    else:
        overall = Outcome.SUCCESS
    return Verdict(overall, per, reasons)
