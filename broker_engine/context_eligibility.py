"""Eligibility of the evidence behind a context response: eligible, ineligible or gap.

Pure functions: no network, no clock reads (times come in as arguments), no verdict.

Three outcomes (D-15). A gap is absent evidence (no goal, placement or observation to judge).
Ineligible is present but unfit evidence (stale, conflicted, changed-goal, duplicated, unmapped,
outside the caller's authority, or ambiguous identity). Eligible means every applicable item is
fresh, finite, single-valued and unchanged. Any single failure beats eligible, so the goals class
fails closed on a changed goal, and the observations class fails closed on a stale or conflicted
sample. Each requested class is judged on its own and lists its own reasons (D-18); only the
classes the request asked for appear, so a topology-only request is never made ineligible by a
stale metric it did not ask for.

Reasons are fixed strings. The only variable part is the short name of a pinned metric URI
(`_short_name`, letters, digits, dot, underscore, hyphen, at most 64 characters), never text from
a server or a caller (D-15).

Eligibility grants no execution authority (the action-readiness check is a separate component) and
is kept apart from the verdict: this module never reads or produces a verdict, and imports nothing
from the verdict path (D-19). The two can disagree on a current request, by design: the verdict
compares the live TMF921 goal with the pinned one, eligibility reads the stored fluents only.
"""
from datetime import datetime, timedelta

from data_contracts.context_contract import ClassEligibility
from data_contracts.fluents import Fluent, select_in_force
from outcome_assurance.tmf921_report import _short_name

# A sample stamped this far past the valid time is future-dated (same tolerance as the joiner).
FUTURE_TOLERANCE_S = 5

# Reasons with no variable part.
NO_BINDING = "no binding for intent"
INVALID_BINDING = "invalid binding"
SCOPE_MISSING = "scope entity not in the topology"
SCOPE_AMBIGUOUS = "ambiguous identity: scope entity"
NODE_AMBIGUOUS = "ambiguous identity: graph node"
LINK_CONFLICT = "conflicting link records"
TOPOLOGY_INVALID = "invalid topology record"

# Reason prefixes; the metric short name follows ": ".
NO_GOAL = "no goal in force"
NO_PLACEMENT = "no placement in force"
DUPLICATE_GOAL = "duplicate goal in force"
DUPLICATE_PLACEMENT = "duplicate placement in force"
GOAL_CHANGED = "goal changed within window"


def _reason(prefix: str, metric: str) -> str:
    return f"{prefix}: {_short_name(metric)}"


def overlapping(fluents, lo: datetime, hi: datetime, known_at: datetime | None) -> list[Fluent]:
    """The distinct fluents known at `known_at` whose valid interval [start, end) meets [lo, hi].

    A fluent meets the window exactly when start <= hi and (end is None or end > lo). A
    correction (supersede and reissue with the same interval) leaves only one of the two known
    at any knowledge time, so it never counts as a second fluent.
    """
    found: list[Fluent] = []
    for fluent in fluents:
        if (fluent.known(known_at) and fluent.start <= hi
                and (fluent.end is None or fluent.end > lo) and fluent not in found):
            found.append(fluent)
    return found


def classify(gaps, problems) -> ClassEligibility:
    """Ineligible when there is any problem, else gap when there is any gap, else eligible.

    Reasons are the problems and gaps together, de-duplicated and sorted.
    """
    reasons = sorted(set(problems) | set(gaps))
    result = "ineligible" if problems else "gap" if gaps else "eligible"
    return ClassEligibility(result=result, reasons=reasons)


def goals_eligibility(fluents, interfaces, metric_map, *, valid_at: datetime,
                      known_at: datetime | None, limit_s: int, allowed) -> ClassEligibility:
    """Judge the goal and placement fluents of an intent for the evidence window
    [valid_at - limit_s, valid_at].

    The goals class fails closed: a goal that changed inside the window, or two distinct goals
    or placements in force at once, make it ineligible (D-16, D-17). A missing goal or placement
    is a gap. No TMF921 read is needed, so a past request reads the graph only.
    """
    if not fluents:
        return classify([NO_BINDING], [])
    gaps: list[str] = []
    problems: list[str] = []
    lo = valid_at - timedelta(seconds=limit_s)
    for metric in sorted({m for m, _kind in fluents}):
        try:
            goal = select_in_force(fluents[(metric, "goal")], valid_at, known_at)
        except ValueError:
            problems.append(_reason(DUPLICATE_GOAL, metric))
        else:
            if goal is None:
                gaps.append(_reason(NO_GOAL, metric))
            if len(overlapping(fluents[(metric, "goal")], lo, valid_at, known_at)) > 1:
                problems.append(_reason(GOAL_CHANGED, metric))
        try:
            place = select_in_force(fluents[(metric, "placement")], valid_at, known_at)
        except ValueError:
            problems.append(_reason(DUPLICATE_PLACEMENT, metric))
        else:
            if place is None:
                gaps.append(_reason(NO_PLACEMENT, metric))
    return classify(gaps, problems)
