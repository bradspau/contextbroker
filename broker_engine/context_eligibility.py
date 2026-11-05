"""Eligibility of the evidence behind a context response: eligible, ineligible or gap.

Pure functions: no network, no clock reads (times come in as arguments), no verdict.

Four classes are judged: topology, goals, observations and events. Three outcomes (D-15). A gap is absent evidence (no goal, placement or observation to judge).
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
import math
from datetime import datetime, timedelta

from data_contracts.context_contract import ClassEligibility, Eligibility
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
NO_EVENTS = "no events in window"
WINDOW_BEYOND_RETENTION = "window reaches beyond event retention"
EVENT_AMBIGUOUS = "ambiguous identity: event source"
TOPOLOGY_INVALID = "invalid topology record"

# Reason prefixes; the metric short name follows ": ".
NO_GOAL = "no goal in force"
NO_PLACEMENT = "no placement in force"
DUPLICATE_GOAL = "duplicate goal in force"
DUPLICATE_PLACEMENT = "duplicate placement in force"
GOAL_CHANGED = "goal changed within window"
PLACEMENT_CHANGED = "placement changed within window"
OUTSIDE_AUTHORITY = "placement outside authority scope"
UNMAPPED = "unmapped metric"
NO_OBSERVATION = "no observation"
CONFLICTING_OBSERVATION = "conflicting observation"
STALE_OBSERVATION = "stale observation"
FUTURE_OBSERVATION = "future-dated observation"

SCOPE_IDENTITIES = ("resolved", "missing", "ambiguous")


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

    The goals class fails closed: a goal or placement that changed inside the window, two
    distinct goals or placements in force at once, a metric the metric map does not know, and a
    placement on a device the caller's authority does not cover all make it ineligible (D-16,
    D-17, D-26); the last means no telemetry will be read for that placement. A missing goal or
    placement is a gap. No TMF921 read is needed, so a past request reads the graph only.
    `metric_map` is keyed by metric URI; `allowed(device)` is the caller's entity check.
    """
    if not fluents:
        return classify([NO_BINDING], [])
    gaps: list[str] = []
    problems: list[str] = []
    lo = valid_at - timedelta(seconds=limit_s)
    for metric in sorted({m for m, _kind in fluents}):
        if metric not in metric_map:
            problems.append(_reason(UNMAPPED, metric))
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
            else:
                device = interfaces.get(place.value, (None, None))[0]
                if device is None or not allowed(device):
                    problems.append(_reason(OUTSIDE_AUTHORITY, metric))
            if len(overlapping(fluents[(metric, "placement")], lo, valid_at, known_at)) > 1:
                problems.append(_reason(PLACEMENT_CHANGED, metric))
    return classify(gaps, problems)


def topology_eligibility(scope_identity: str, *, conflicting_links: bool = False,
                         colliding_ids: bool = False, malformed: bool = False) -> ClassEligibility:
    """Judge the topology read. `scope_identity` is "resolved", "missing" or "ambiguous".

    The topology class fails closed on an ambiguous scope entity, two link records on one link,
    node ids that collide, and a malformed record (D-17). A scope entity absent from the graph is
    a gap. A resolved scope with no link valid then is still eligible.
    """
    if scope_identity not in SCOPE_IDENTITIES:
        raise ValueError("unknown scope identity")
    gaps = [SCOPE_MISSING] if scope_identity == "missing" else []
    problems = [SCOPE_AMBIGUOUS] if scope_identity == "ambiguous" else []
    if conflicting_links:
        problems.append(LINK_CONFLICT)
    if colliding_ids:
        problems.append(NODE_AMBIGUOUS)
    if malformed:
        problems.append(TOPOLOGY_INVALID)
    return classify(gaps, problems)


def events_eligibility(*, count: int, ambiguous: int, beyond_retention: bool) -> ClassEligibility:
    """Judge the events returned for the window.

    No event in the window is a gap, and so is a window that reaches back beyond the event retention
    (the kept part cannot show everything the window asked for) (D-31). An ambiguous event is present
    but unfit: its device is not known (D-09). `time_unknown` and a cut by the result cap are quality
    flags the result shows, not reasons here (D-32), and a conflict is never an input (D-15)."""
    gaps = []
    if count == 0:
        gaps.append(NO_EVENTS)
    if beyond_retention:
        gaps.append(WINDOW_BEYOND_RETENTION)
    problems = [EVENT_AMBIGUOUS] if ambiguous > 0 else []
    return classify(gaps, problems)


def binding_problem(reason: str) -> ClassEligibility:
    """The goals class when the binding could not be read: a gap when the intent has none,
    ineligible when the stored binding is invalid (a multiplicity fails closed, D-17)."""
    if reason == NO_BINDING:
        return classify([NO_BINDING], [])
    if reason == INVALID_BINDING:
        return classify([], [INVALID_BINDING])
    raise ValueError("unknown binding reason")


def _finite_value(envelope) -> bool:
    value = envelope.value
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, (int, str))


def observations_eligibility(metrics, observed, *, valid_at: datetime,
                             limit_s: int) -> ClassEligibility:
    """Judge one envelope per expected metric URI against the valid time `valid_at`.

    The observations class fails closed. Absent evidence is a gap: the metric is not in
    `observed`, or its envelope is missing, has no value, or holds a non-finite number (D-29;
    the joiner already reports NaN as missing and is not changed). Present but unfit evidence is
    ineligible: more than one series (conflicts), the stale flag, a sample older than `limit_s`
    (the joiner judges against 300 s only, so a tightened limit is applied here), or one stamped
    more than FUTURE_TOLERANCE_S after `valid_at`. A conflict's own text is never echoed.
    """
    gaps: list[str] = []
    problems: list[str] = []
    for metric in metrics:
        envelope = observed.get(metric)
        if (envelope is None or envelope.quality.missing or not _finite_value(envelope)):
            gaps.append(_reason(NO_OBSERVATION, metric))
            continue
        if envelope.quality.conflicts:
            problems.append(_reason(CONFLICTING_OBSERVATION, metric))
        at = envelope.observed_at
        if at is None:
            # A sample with no time cannot be shown fresh.
            problems.append(_reason(STALE_OBSERVATION, metric))
            continue
        age_s = (valid_at - at).total_seconds()
        if envelope.quality.stale or age_s > limit_s:
            problems.append(_reason(STALE_OBSERVATION, metric))
        if -age_s > FUTURE_TOLERANCE_S:
            problems.append(_reason(FUTURE_OBSERVATION, metric))
    return classify(gaps, problems)


def overall(classes: dict) -> Eligibility:
    """Ineligible if any requested class is, else gap if any is, else eligible.

    Reasons are the sorted union; each class keeps its own. Only classes present in `classes`
    count (D-18).
    """
    results = {c.result for c in classes.values()}
    result = "ineligible" if "ineligible" in results else "gap" if "gap" in results else "eligible"
    reasons = sorted({r for c in classes.values() for r in c.reasons})
    return Eligibility(result=result, classes=dict(classes), reasons=reasons)
