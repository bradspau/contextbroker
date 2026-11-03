"""Interpret TMF921 intent reports (JSON handling state, optional TR290 Turtle) as broker verdicts.

Verdict mapping: ``Verdict.per_interface`` carries per-condition entries
``{"condition", "outcome", "reason"}``; ``Verdict.reasons`` carries fixed strings only.
Raw server text is never echoed into reasons.
"""
import logging
import re
from datetime import datetime, timedelta

from broker_engine.faults import log_fault
from data_contracts.envelope import DEFAULT_MAX_AGE_S
from outcome_assurance.evaluate import Outcome, Verdict

log = logging.getLogger(__name__)

MAX_TURTLE_BYTES = 64 * 1024
FUTURE_SKEW = timedelta(seconds=5)
_PREFIX = "Conditions not met: "
_SAFE = re.compile(r"[^A-Za-z0-9_.\-]")


def _inconclusive(reason: str) -> Verdict:
    return Verdict(Outcome.INCONCLUSIVE,
                   [{"condition": "report", "outcome": Outcome.INCONCLUSIVE.value, "reason": reason}],
                   [reason])


def _short_name(uri) -> str:
    s = str(uri)
    s = s.split("#")[-1] if "#" in s else s.rstrip("/").split("/")[-1]
    return _SAFE.sub("_", s)[:64] or "unknown"


def _classify(clause: str) -> tuple[Outcome, str]:
    if "no observation:" in clause:
        return Outcome.INCONCLUSIVE, "no observation"
    if "unit mismatch:" in clause:
        return Outcome.INCONCLUSIVE, "unit mismatch"
    if clause.endswith(": FAIL"):
        return Outcome.FAILURE, "goal not met"
    return Outcome.INCONCLUSIVE, "unrecognised clause"


def _combine(outcomes) -> Outcome:
    outcomes = set(outcomes)
    if Outcome.FAILURE in outcomes:
        return Outcome.FAILURE
    if Outcome.INCONCLUSIVE in outcomes or not outcomes:
        return Outcome.INCONCLUSIVE
    return Outcome.SUCCESS


def _json_conditions(state, reason) -> list[tuple[Outcome, str]]:
    if state == "Fulfilled":
        if reason is None:
            return [(Outcome.SUCCESS, "fulfilled")]
        return [(Outcome.INCONCLUSIVE, "inconsistent report")]
    if state == "Degraded":
        if not reason or not reason.startswith(_PREFIX):
            return [(Outcome.INCONCLUSIVE, "missing or garbled reason")]
        clauses = [c.strip() for c in reason[len(_PREFIX):].split("; ") if c.strip()]
        if not clauses:
            return [(Outcome.INCONCLUSIVE, "missing or garbled reason")]
        return [_classify(c) for c in clauses]
    return [(Outcome.INCONCLUSIVE, "unknown state")]


def _turtle_outcome(text: str) -> Outcome | None:
    """Outcome from TR290 ConditionReports, or None if unparseable."""
    try:
        if len(text.encode("utf-8")) > MAX_TURTLE_BYTES:
            return None
        import rdflib
        from rdflib.namespace import RDF, RDFS
        g = rdflib.Graph()
        g.parse(data=text, format="turtle")

        def local(u):
            return str(u).split("#")[-1].rsplit("/", 1)[-1]

        outcomes = []
        for s, _, c in g.triples((None, RDF.type, None)):
            if local(c) != "ConditionReport":
                continue
            result = reason = None
            for _, p, o in g.triples((s, None, None)):
                if local(p) == "result":
                    result = str(o).strip().lower()
                elif local(p) == "reason":
                    if isinstance(o, rdflib.Literal):
                        reason = str(o)
                    else:
                        reason = str(g.value(o, RDFS.label) or "")
            if result == "true":
                outcomes.append(Outcome.SUCCESS)
            elif result == "false":
                r = (reason or "").strip()
                if r.startswith("no observation") or r.startswith("unit mismatch"):
                    outcomes.append(Outcome.INCONCLUSIVE)
                else:
                    outcomes.append(Outcome.FAILURE)
            else:
                outcomes.append(Outcome.INCONCLUSIVE)
        return _combine(outcomes)
    except Exception as exc:
        log_fault(log, exc, "report Turtle unreadable")
        return None


def _stale_metric(observed_at, now, max_age_s) -> str | None:
    if not isinstance(observed_at, dict) or not observed_at:
        return "no-freshness-evidence"
    for uri, ts in observed_at.items():
        name = _short_name(uri)
        if not isinstance(ts, datetime) or ts.tzinfo is None or ts.utcoffset() is None:
            return name
        age = now - ts
        if age > timedelta(seconds=max_age_s) or -age > FUTURE_SKEW:
            return name
    return None


def interpret_report(report, *, now: datetime, max_age_s: int = DEFAULT_MAX_AGE_S,
                     observed_at: dict | None = None) -> Verdict:
    try:
        return _interpret(report, now, max_age_s, observed_at)
    except Exception as exc:
        log_fault(log, exc, "unparseable report")
        return _inconclusive("unparseable report")


def _interpret(report, now, max_age_s, observed_at) -> Verdict:
    if (not isinstance(report, dict) or not isinstance(now, datetime)
            or now.tzinfo is None or now.utcoffset() is None):
        return _inconclusive("invalid input")
    state = report.get("intentHandlingState")
    reason = report.get("intentHandlingReason")
    expression = report.get("expression")
    if not isinstance(state, str) or not (reason is None or isinstance(reason, str)):
        return _inconclusive("invalid input")
    if expression is not None and not isinstance(expression, dict):
        return _inconclusive("invalid input")

    conds = _json_conditions(state, reason)
    per = [{"condition": f"condition-{i + 1}", "outcome": o.value, "reason": r}
           for i, (o, r) in enumerate(conds)]
    overall = _combine(o for o, _ in conds)
    reasons = [r for o, r in conds if o is not Outcome.SUCCESS]

    if isinstance(expression, dict) and isinstance(expression.get("expressionValue"), str):
        t = _turtle_outcome(expression["expressionValue"])
        if t is None:
            return _inconclusive("unparseable report")
        if t is not overall:
            per.append({"condition": "turtle", "outcome": Outcome.INCONCLUSIVE.value,
                        "reason": "report disagreement"})
            return Verdict(Outcome.INCONCLUSIVE, per, reasons + ["report disagreement"])

    if overall is Outcome.SUCCESS:
        stale = _stale_metric(observed_at, now, max_age_s)
        if stale is not None:
            reason_s = f"stale observation: {stale}"
            per.append({"condition": "freshness", "outcome": Outcome.INCONCLUSIVE.value,
                        "reason": reason_s})
            return Verdict(Outcome.INCONCLUSIVE, per, [reason_s])
    return Verdict(overall, per, reasons)
