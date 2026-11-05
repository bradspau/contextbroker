"""Assess an intent through the TMF921 server: bind, push, poll, interpret.

`assess_tmf921_as_of` is the read-only temporal variant (as_of, known_at, mode): it
evaluates telemetry at a past instant or over a window in the broker, against the goal
and placement in force then as recorded in the network graph, and pushes nothing.

The TMF921 assess fails closed: stale, missing, conflicted, future-dated or unmappable evidence is
never a success, and nothing is pushed unless every bound metric is pushable.
Server text never reaches the result (fixed reason strings only).

Known limitation: the server exposes no used-observation time, so pushes made
by other clients (including future-dated ones) cannot be seen. The broker keeps a
durable per-(intent, metric) high-water mark of what IT pushed, in the state
store. It survives restarts, and the broker refuses to push anything older (or
equal with a different value). One API worker is the supported setup (D-02).
"""
import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from fastapi.encoders import jsonable_encoder

from broker_engine.binding_resolver import resolve_binding
from broker_engine.faults import log_fault
from broker_engine.pruner import prune
from broker_engine.temporal import temporal_observation
from broker_engine.tsdb_joiner import join_observed_metric
from broker_engine.validation import (
    _SAFE_WINDOW,
    _UNIT_SECONDS,
    ClientInputError,
    window_seconds,
)
from data_contracts.envelope import DEFAULT_MAX_AGE_S, build_observed
from data_contracts.intent_bindings import IntentBinding
from data_contracts.metric_map import load_metric_map
from intent_management.store import IntentStore
from intent_management.tmf921_client import (
    Tmf921Client,
    _check_intent_id,
    intent_bounds,
)
from outcome_assurance.evaluate import OPERATORS, Outcome, evaluate_intent
from outcome_assurance.tmf921_report import _short_name, interpret_report

log = logging.getLogger(__name__)

FUTURE_SKEW = timedelta(seconds=5)

# Last outcome the broker reported per TMF921 intent (durable, in the state store).
TMF921_STORE = IntentStore()
_STATE_LOCK = threading.Lock()
_INTENT_LOCKS: dict[str, threading.Lock] = {}
_TS = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)")

DEFAULT_MAX_ENTITIES = 50


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _intent_lock(intent_id: str) -> threading.Lock:
    with _STATE_LOCK:
        return _INTENT_LOCKS.setdefault(intent_id, threading.Lock())


def _record(intent_id: str, outcome: Outcome) -> None:
    # The cap and its eviction signal live in the state store (D-11, D-12).
    TMF921_STORE.fulfilment_update(intent_id, outcome)


def _parse_ts(value) -> datetime | None:
    """Parse a Java Instant string (variable fraction digits) or None."""
    if not isinstance(value, str):
        return None
    m = _TS.fullmatch(value)
    if not m:
        return None
    frac = (m.group(2) or "").ljust(6, "0")[:6]
    zone = "+00:00" if m.group(3) == "Z" else m.group(3)
    try:
        dt = datetime.fromisoformat(f"{m.group(1)}.{frac}{zone}")
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _result(outcome: Outcome, reasons, per_interface=None, envelopes=(), previous=None,
            status="active",
            goal_changes=()):
    pruned = prune(list(envelopes), max(DEFAULT_MAX_ENTITIES, len(envelopes)))
    out = {
        "verdict": outcome.value,
        "per_interface": per_interface if per_interface is not None else [
            {"condition": "intent", "outcome": outcome.value, "reason": r} for r in reasons],
        "reasons": list(reasons),
        "context": [e.to_dict() for e in pruned.kept],
        "dropped_count": pruned.dropped_count,
        "intent_status": status,
        "last_outcome": previous,
        "source": "tmf921",
    }
    if goal_changes:
        out["goal_changes"] = list(goal_changes)
    return jsonable_encoder(out)


def _unpushable(env, ref: datetime) -> str | None:
    q = env.quality
    if q.missing or env.value is None:
        return "missing observation"
    if isinstance(env.value, bool) or not isinstance(env.value, (int, float)) \
            or not math.isfinite(env.value):
        return "invalid observation"
    if q.stale or env.observed_at is None:
        return "stale observation"
    if env.observed_at - ref > FUTURE_SKEW:
        return "future-dated observation"
    if q.conflicts:
        return "conflicting observation"
    return None


def _is_new(report, pre_ids) -> bool:
    rid = report.get("id")
    return isinstance(rid, str) and rid not in pre_ids \
        and _parse_ts(report.get("creationDate")) is not None


def _bounds_match(doc, binding) -> bool:
    current = intent_bounds(doc)
    return bool(binding.bounds) and current.keys() == binding.bounds.keys() \
        and all(current[u] == b for u, b in binding.bounds.items())


def _goal_changes(doc, binding) -> tuple[list[str], list[dict]]:
    """Describe how the server's goal differs from the pin: (reasons, changes).

    Called only after _bounds_match is false. Built from the pinned metric short
    names and the numbers in intent_bounds, never from server text or a
    server-supplied metric name. Goals that cannot be read at all give "intent goal unreadable".
    """
    current = intent_bounds(doc)
    if not current:
        return ["intent goal unreadable"], []
    reasons, changes = [], []
    for uri, pinned in binding.bounds.items():
        if uri in current and current[uri] != pinned:
            name = _short_name(uri)
            reasons.append(f"goal changed: {name} pinned {format(pinned, '.15g')}, "
                           f"now {format(current[uri], '.15g')}")
            changes.append({"metric": name, "pinned": float(pinned),
                            "current": float(current[uri])})
    if current.keys() != binding.bounds.keys():
        reasons.append("goal set changed")
    return reasons or ["intent goal unreadable"], changes


def _gap_reasons(binding) -> list[str]:
    """One fixed reason per gap, named by the pinned metric's short name.

    A gap is a metric the broker recorded no goal or no placement for at the requested
    time; the assess is inconclusive for the whole intent, never partial.
    """
    return [f"no {kind} in force: {_short_name(uri)}" for uri, kind in binding.gaps]


def _binding_for(intent_id: str, bindings=None, *, valid_at=None,
                 known_at=None) -> tuple[IntentBinding | None, str | None]:
    """Find the binding for an intent: (binding, None) or (None, fixed reason).

    `valid_at` and `known_at` (aware datetimes) select the goal and placement in force
    then as known then; they are passed to the resolver only when given, so the
    current-instant path still calls resolve_binding(intent_id) exactly as before.

    Reasons: "no binding for intent", "invalid binding", "binding store unavailable".
    An explicit `bindings` mapping is used alone. Otherwise the only source is the
    `:ServiceBinding` nodes in the network graph. If the graph is unreachable (or
    replies with something that is not JSON) the reason is "binding store
    unavailable"; a malformed or duplicate graph binding is "invalid binding"; no
    binding is "no binding for intent". In each case the TMF921 assess returns
    inconclusive and pushes nothing. Fuseki error text is never returned.
    """
    if bindings is not None:
        binding = bindings.get(intent_id)
        return (binding, None) if binding else (None, "no binding for intent")
    try:
        if valid_at is None and known_at is None:
            binding = resolve_binding(intent_id)
        else:
            binding = resolve_binding(intent_id, valid_at=valid_at, known_at=known_at)
    except requests.RequestException:  # checked first: includes a non-JSON reply
        return None, "binding store unavailable"
    except ClientInputError:  # unsafe caller input is not a binding problem
        raise
    except ValueError:
        return None, "invalid binding"
    if binding is None:
        return None, "no binding for intent"
    return binding, None


def assess_via_tmf921(
    intent_id: str,
    time_window: str,
    *,
    client: Tmf921Client | None = None,
    join=None,
    now=None,
    metric_map=None,
    bindings=None,
    poll_interval_s: float = 0.2,
    poll_timeout_s: float = 5.0,
    sleep=time.sleep,
    clock=_utcnow,
    monotonic=time.monotonic,
) -> dict:
    # Caller input first: a bad value is a client error before any HTTP.
    _check_intent_id(intent_id)
    if not isinstance(time_window, str) or not _SAFE_WINDOW.match(time_window):
        raise ClientInputError(f"Unsafe time_window: {time_window!r}")
    join = join or join_observed_metric

    binding, reason = _binding_for(intent_id, bindings)
    if binding is None:
        return _result(Outcome.INCONCLUSIVE, [reason], status="unknown")
    if binding.gaps:  # before the lock, any TMF921 call, a push or a mark; nothing is recorded
        return _result(Outcome.INCONCLUSIVE, _gap_reasons(binding), status="unknown")
    try:
        metric_map = load_metric_map() if metric_map is None else metric_map
    except ValueError:
        return _result(Outcome.INCONCLUSIVE, ["invalid binding"], status="unknown")
    client = client or Tmf921Client()
    with _intent_lock(intent_id):
        return _assess_locked(intent_id, time_window, binding, client, join, now,
                              metric_map, poll_interval_s, poll_timeout_s, sleep,
                              clock, monotonic)


def _assess_locked(intent_id, time_window, binding, client, join, now, metric_map,
                   poll_interval_s, poll_timeout_s, sleep, clock, monotonic) -> dict:
    prev = TMF921_STORE.last_outcome(intent_id)
    previous = prev.value if prev else None

    def rejected(reason):  # caller/config input rejected: not an assessment
        return _result(Outcome.INCONCLUSIVE, [reason], previous=previous, status="unknown")

    def done(outcome, reasons, per=None, envs=(), status="active", changes=()):
        _record(intent_id, outcome)
        return _result(outcome, reasons, per, envs, previous, status, changes)

    # Goal check before anything is pushed: same metric set and values as the pin.
    try:
        doc = client.get_intent(intent_id)
        ok = _bounds_match(doc, binding)
    except ClientInputError:
        return rejected("invalid binding")
    except Exception as exc:
        log_fault(log, exc, "intent server unavailable")
        return done(Outcome.INCONCLUSIVE, ["intent server unavailable"])
    if not ok:
        reasons, changes = _goal_changes(doc, binding)
        return done(Outcome.INCONCLUSIVE, reasons, status="unknown", changes=changes)

    uris = list(binding.bounds)
    mappings = {}
    for uri in uris:
        m = metric_map.get(uri)
        if m is None or uri not in binding.sources:
            return done(Outcome.INCONCLUSIVE, [f"unmapped metric: {_short_name(uri)}"])
        mappings[uri] = m

    envs = {}
    for uri in uris:
        device, interface = binding.sources[uri]
        try:
            envs[uri] = join(device, interface, mappings[uri].broker_metric, time_window,
                              aggregate="last")
        except ClientInputError:
            return rejected("invalid binding")
    all_envs = list(envs.values())

    ref = now or clock()
    problems = [f"{why}: {_short_name(u)}" for u, e in envs.items()
                if (why := _unpushable(e, ref))]
    if problems:  # all-or-nothing: never push a partial set
        return done(Outcome.INCONCLUSIVE, problems, envs=all_envs)

    marks = TMF921_STORE.marks(intent_id, uris)  # one read; a store failure leaves before any post
    regress = [u for u in uris if (m := marks.get(u)) is not None
               and not (envs[u].observed_at > m[0]
                        or (envs[u].observed_at == m[0] and envs[u].value == m[1]))]
    if regress:
        return done(Outcome.INCONCLUSIVE,
                    [f"observation not newer than last pushed: {_short_name(u)}" for u in regress],
                    envs=all_envs)

    observed_at = {u: envs[u].observed_at for u in uris}
    pushed: list[str] = []  # short names of the metrics whose post succeeded
    try:
        pre_ids = {r.get("id") for r in client.list_reports(intent_id)}
        for u in uris:
            try:
                client.post_observation(intent_id, u, envs[u].value, mappings[u].unit,
                                        envs[u].observed_at)
            except Exception as exc:
                if not pushed:  # nothing reached the server yet: today's results
                    if isinstance(exc, ClientInputError):
                        return rejected("invalid observation")
                    raise
                # Part-way through: report which pinned metrics went in and which
                # failed. Nothing is retried and nothing already pushed is undone;
                # a retry is the caller's choice (the high-water marks stop a stale one).
                log_fault(log, exc, "partial push")
                return done(Outcome.INCONCLUSIVE,
                            ["partial push: pushed " + ", ".join(pushed)
                             + "; failed " + _short_name(u)],
                            envs=all_envs)
            # Only after the post succeeded: a failed post leaves no mark.
            if not TMF921_STORE.advance_mark(intent_id, u, envs[u].observed_at, envs[u].value):
                log.warning("high-water mark not advanced: %s", _short_name(u))
            pushed.append(_short_name(u))
        report = None
        waited = 0.0
        start = monotonic()
        while True:
            new = sorted((r for r in client.list_reports(intent_id) if _is_new(r, pre_ids)),
                         key=lambda r: _parse_ts(r["creationDate"]), reverse=True)
            if len(new) >= len(uris):
                if len(new) > 1 and _parse_ts(new[0]["creationDate"]) == _parse_ts(
                        new[1]["creationDate"]):
                    return done(Outcome.INCONCLUSIVE, ["ambiguous report order"],
                                envs=all_envs)
                report = new[0]
                break
            if waited >= poll_timeout_s or monotonic() - start >= poll_timeout_s:
                break
            sleep(poll_interval_s)
            waited += poll_interval_s
    except Exception as exc:  # Tmf921Error and anything unexpected: fail closed
        log_fault(log, exc, "intent server unavailable")
        return done(Outcome.INCONCLUSIVE, ["intent server unavailable"], envs=all_envs)
    if report is None:
        return done(Outcome.INCONCLUSIVE, ["no new report"], envs=all_envs)

    verdict = interpret_report(report, now=now or clock(), max_age_s=DEFAULT_MAX_AGE_S,
                               observed_at=observed_at)
    outcome, reasons, per = verdict.outcome, list(verdict.reasons), list(verdict.per_interface)
    if outcome is Outcome.SUCCESS:
        # The server rewrites a stored goal when it evaluates Degraded: confirm
        # the goal still equals the pinned one before trusting a success.
        try:
            doc = client.get_intent(intent_id)
            still = _bounds_match(doc, binding)
        except Exception as exc:
            log_fault(log, exc, "intent server unavailable")
            return done(Outcome.INCONCLUSIVE, ["intent server unavailable"], envs=all_envs)
        if not still:
            reasons, changes = _goal_changes(doc, binding)
            return done(Outcome.INCONCLUSIVE, reasons, envs=all_envs, status="unknown",
                        changes=changes)
    return done(outcome, reasons, per, all_envs)


TEMPORAL_REQUIREMENT = ("goal in force now, pinned in the network graph and checked "
                        "against the TMF921 intent")
HISTORICAL_REQUIREMENT = "goal in force at the requested time, as recorded in the network graph"


def _goal_entries(binding) -> list[dict]:
    """One entry per judged metric, by short name: the goal and the interval it holds.

    Numbers, times and pinned short names only. An injected test binding carries no
    intervals, so its times are null.
    """
    entries = []
    for uri, value in binding.bounds.items():
        start, end = binding.goal_intervals.get(uri, (None, None))
        entries.append({"metric": _short_name(uri), "value": float(value),
                        "valid_from": start, "valid_to": end})
    return sorted(entries, key=lambda e: e["metric"])


def assess_tmf921_as_of(
    intent_id: str,
    time_window: str,
    *,
    as_of: str | None,
    mode: str | None,
    known_at: str | None = None,
    client: Tmf921Client | None = None,
    join=None,
    now: datetime | None = None,
    metric_map=None,
    bindings=None,
) -> dict:
    """Temporal assess of a TMF921 intent: judged in the broker, nothing pushed.

    `as_of` ("10m") is an offset back from now; `known_at` ("1h") is an offset back
    from now for what the broker had recorded by then. `mode` is "at" (the default:
    the last sample at that instant, judged fresh relative to it) or "within" (the
    worst case over `time_window` ending then, in the direction of each goal's
    operator from metric_map.json). `mode` without `as_of` ends now.

    A past question (`as_of` or `known_at` given) reads the goal and the placement
    in force at the as_of instant, as known at the known_at instant (latest when
    absent), from the network graph's fluents only. The TMF921 server is not asked
    at all: its live goal says nothing about the goal then, and an intent that has
    ended stays answerable. The result names the goals used (`goals`, with
    valid_from and valid_to) and the knowledge time (`known_at`). Without either
    argument (`mode` only) the question is about now: the goal in force now is also
    checked against the TMF921 intent and a non-active intent is inconclusive.

    Read-only: no observation is pushed, no high-water mark is written and the
    stored fulfilment outcome is not updated (`last_outcome` is that stored value,
    read once). No per-intent lock is taken: the lock in assess_via_tmf921 orders
    the push/mark/report sequence, none of which happens here, and a concurrent
    push only changes which stored outcome is reported as `last_outcome`.

    The temporal TMF921 assess fails closed: unmapped metrics, a metric without an
    operator, a time with no goal or no placement in force, a goal or placement
    that started inside a "within" window, a current goal that differs from the
    pinned one, an unreachable server or binding store, and missing, stale,
    conflicting or non-finite evidence are never a success. Reasons are fixed
    strings; server text never reaches the result.
    """
    # Caller input first: a bad value is a client error before any I/O.
    _check_intent_id(intent_id)
    if not isinstance(time_window, str) or not _SAFE_WINDOW.match(time_window):
        raise ClientInputError(f"Unsafe time_window: {time_window!r}")
    if mode not in (None, "at", "within"):
        raise ClientInputError(f"Unknown mode: {mode!r}")
    offset_s = window_seconds(as_of, "as_of") if as_of is not None else 0
    known_s = window_seconds(known_at, "known_at") if known_at is not None else None
    historical = as_of is not None or known_at is not None
    mode = mode or "at"
    now = now or _utcnow()
    at = now - timedelta(seconds=offset_s)
    known = now - timedelta(seconds=known_s) if known_s is not None else None
    join = join or join_observed_metric
    prev = TMF921_STORE.last_outcome(intent_id)
    previous = prev.value if prev else None
    goals: list[dict] = []

    def result(outcome, reasons, per=None, envs=(), status="active", changes=()):
        # The lifecycle is not read for a past question.
        out = _result(outcome, reasons, per, envs, previous,
                      "unknown" if historical else status, changes)
        extra = {"mode": mode, "as_of": as_of, "evaluated_at": at,
                 "requirement": HISTORICAL_REQUIREMENT if historical else TEMPORAL_REQUIREMENT}
        if historical:
            extra.update({"known_at": known or now, "goals": goals})
        out.update(jsonable_encoder(extra))
        return out

    if historical:
        binding, reason = _binding_for(intent_id, bindings, valid_at=at, known_at=known)
    else:
        binding, reason = _binding_for(intent_id, bindings)
    if binding is None:
        return result(Outcome.INCONCLUSIVE, [reason], status="unknown")
    if historical:
        goals = _goal_entries(binding)
    if binding.gaps:  # before any TMF921 call or telemetry read
        return result(Outcome.INCONCLUSIVE, _gap_reasons(binding), status="unknown")
    try:
        metric_map = load_metric_map() if metric_map is None else metric_map
    except ValueError:
        return result(Outcome.INCONCLUSIVE, ["invalid binding"], status="unknown")
    if not historical:
        client = client or Tmf921Client()
        try:
            doc = client.get_intent(intent_id)
            ok = _bounds_match(doc, binding)
        except ClientInputError:
            return result(Outcome.INCONCLUSIVE, ["invalid binding"], status="unknown")
        except Exception as exc:  # Tmf921Error and anything unexpected: no server text
            log_fault(log, exc, "intent server unavailable")
            return result(Outcome.INCONCLUSIVE, ["intent server unavailable"])
        if not ok:
            reasons, changes = _goal_changes(doc, binding)
            return result(Outcome.INCONCLUSIVE, reasons, status="unknown", changes=changes)

        life = doc.get("lifecycleStatus") if isinstance(doc, dict) else None
        if life != "ACTIVE":  # before any telemetry is read; server text is never echoed
            known_life = life if life in ("ACKNOWLEDGED", "SUSPENDED", "TERMINATED") else "unknown"
            return result(Outcome.INCONCLUSIVE, ["intent not active"], status=known_life)

    # A "within" window runs back from the as_of instant; a goal or placement recorded
    # as starting inside it would judge the earlier samples against a different (or no)
    # goal, so that metric is inconclusive.
    window_start = at - timedelta(seconds=int(time_window[:-1]) * _UNIT_SECONDS[time_window[-1]])
    per, reasons, envs, outcomes = [], [], [], set()
    for uri, bound in binding.bounds.items():
        name = _short_name(uri)
        device, interface = binding.sources.get(uri, (None, None))
        mapping = metric_map.get(uri)
        goal_start = binding.goal_intervals.get(uri, (None, None))[0]
        place_start = binding.placement_intervals.get(uri, (None, None))[0]
        if mapping is None or device is None:
            problem = f"unmapped metric: {name}"
        elif mapping.operator not in OPERATORS:
            problem = f"no operator for metric: {name}"
        elif mode == "within" and goal_start is not None and goal_start > window_start:
            problem = f"goal not in force for the whole window: {name}"
        elif mode == "within" and place_start is not None and place_start > window_start:
            problem = f"placement not in force for the whole window: {name}"
        else:
            problem = None
        if problem:
            outcomes.add(Outcome.INCONCLUSIVE)
            reasons.append(problem)
            per.append({"condition": name, "device": device, "interface_name": interface,
                        "outcome": Outcome.INCONCLUSIVE.value, "observed": None,
                        "reason": problem})
            continue
        spec = {"metric": mapping.broker_metric, "operator": mapping.operator,
                "target_value": bound,
                "interfaces": [{"device": device, "interface_name": interface}]}
        try:
            env = temporal_observation(join, device, interface, spec, time_window, mode,
                                       as_of, now)
        except ClientInputError:
            # A binding identifier the joiner refuses: this metric is inconclusive.
            env = build_observed(f"{device}:{interface}", None, "victoriametrics", None, at)
            env.quality.conflicts.append("invalid interface identifier")
        if isinstance(env.value, float) and not math.isfinite(env.value):
            env.quality.conflicts.append("invalid observation")  # e.g. +Inf from the TSDB
        envs.append(env)
        verdict = evaluate_intent(spec, {(device, interface): env})
        outcomes.add(verdict.outcome)
        reasons.extend(f"{name}: {r}" for r in verdict.reasons)
        per.extend({"condition": name, **p,
                    "reason": f"{name}: {p['reason']}" if p["reason"] else None}
                   for p in verdict.per_interface)

    if Outcome.FAILURE in outcomes:
        outcome = Outcome.FAILURE
    elif Outcome.INCONCLUSIVE in outcomes or not outcomes:
        outcome = Outcome.INCONCLUSIVE
    else:
        outcome = Outcome.SUCCESS
    return result(outcome, reasons, per, envs)
