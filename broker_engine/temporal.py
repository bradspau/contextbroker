"""Temporal observation for one interface: point-in-time or worst case over a window.

Shared by both assess sources (graph and tmf921). Lives in its own module so
intent_management.tmf921_source can import it without importing broker_engine.assess
(which imports tmf921_source).
"""

# The direction in which each operator is breached: <, <= at the window maximum;
# >, >= at the window minimum. "==" is handled separately (both extremes).
_WORST_CASE = {"<": "max", "<=": "max", ">": "min", ">=": "min"}


def temporal_observation(join, device, interface, intent, time_window, mode, as_of, now):
    """Return the envelope that decides a temporal assessment of one interface.

    mode "at": the last sample at the as_of instant (now when as_of is None),
    judged fresh relative to that instant by the joiner. mode "within": the worst
    case over `time_window` ending at that instant, chosen from intent["operator"].
    """
    metric = intent["metric"]
    kw = {"as_of": as_of, "now": now}
    if mode == "at":
        return join(device, interface, metric, time_window, aggregate="last", **kw)
    op, target = intent.get("operator"), intent.get("target_value")
    if op != "==":
        # <, <= breach at the maximum; >, >= at the minimum. An unknown operator
        # falls through to max and the evaluator reports it as inconclusive.
        return join(device, interface, metric, time_window,
                    aggregate=_WORST_CASE.get(op, "max"), **kw)
    # "== target" is breached by a sample on either side: look at both extremes.
    hi = join(device, interface, metric, time_window, aggregate="max", **kw)
    lo = join(device, interface, metric, time_window, aggregate="min", **kw)
    for env in (hi, lo):
        q = env.quality
        if env.value is None or q.missing or q.stale or q.conflicts:
            return env  # unusable evidence must not be hidden by the other extreme
    return hi if hi.value != target else lo
