"""Assess an intent through the TMF921 server: bind, push, poll, interpret.

`assess_tmf921_as_of` is the read-only temporal variant (as_of, mode): it reads
the bounds from the intent now, evaluates telemetry at a past instant or over a
window in the broker, and pushes nothing.

The TMF921 assess fails closed: stale, missing, conflicted, future-dated or unmappable evidence is
never a success, and nothing is pushed unless every bound metric is pushable.
Server text never reaches the result (fixed reason strings only).

Known limitation: the server exposes no used-observation time, so pushes made
by other clients (including future-dated ones) cannot be seen. The broker only
keeps an in-memory per-(intent, metric) high-water mark of what IT pushed and
refuses to push anything older (or equal with a different value).
"""
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from fastapi.encoders import jsonable_encoder

from broker_engine.binding_resolver import resolve_binding
from broker_engine.pruner import prune
from broker_engine.temporal import temporal_observation
from broker_engine.tsdb_joiner import join_observed_metric
from broker_engine.validation import _SAFE_WINDOW, ClientInputError, window_seconds
from data_contracts.envelope import DEFAULT_MAX_AGE_S, build_observed
from data_contracts.intent_bindings import IntentBinding, load_bindings
from data_contracts.metric_map import load_metric_map
from intent_management.store import IntentStore
from intent_management.tmf921_client import (
    Tmf921Client,
    _check_intent_id,
    intent_bounds,
)
from outcome_assurance.evaluate import OPERATORS, Outcome, evaluate_intent
from outcome_assurance.tmf921_report import _short_name, interpret_report

MAX_TRACKED_OUTCOMES = 1024
FUTURE_SKEW = timedelta(seconds=5)

# Separate from the graph path's store so a graph intent never inherits these.
TMF921_STORE = IntentStore(lambda _i: {})
_STATE_LOCK = threading.Lock()
_INTENT_LOCKS: dict[str, threading.Lock] = {}
_MARKS: dict[tuple[str, str], tuple[datetime, float]] = {}
_TS = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)")

DEFAULT_MAX_ENTITIES = 50


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _intent_lock(intent_id: str) -> threading.Lock:
    with _STATE_LOCK:
        return _INTENT_LOCKS.setdefault(intent_id, threading.Lock())


def _record(intent_id: str, outcome: Outcome) -> None:
    with _STATE_LOCK:
        if (TMF921_STORE.last_outcome(intent_id) is None
                and TMF921_STORE.tracked_count() >= MAX_TRACKED_OUTCOMES):
            return
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


def _binding_for(intent_id: str, bindings=None) -> tuple[IntentBinding | None, str | None]:
    """Find the binding for an intent: (binding, None) or (None, fixed reason).

    Reasons: "no binding for intent", "invalid binding", "binding store unavailable".
    Precedence: an explicit `bindings` mapping is used alone. Otherwise the network
    graph wins; the JSON file is the fallback when the graph has no binding for the
    intent. If the graph is unreachable the JSON file is used only if it has the
    intent, else the reason is "binding store unavailable". A malformed graph
    binding is "invalid binding" and does not fall back to JSON, so the TMF921
    assess returns inconclusive instead of silently using a different binding.
    Fuseki error text is never returned.
    """
    if bindings is not None:
        binding = bindings.get(intent_id)
        return (binding, None) if binding else (None, "no binding for intent")
    unreachable = False
    try:
        binding = resolve_binding(intent_id)
    except requests.RequestException:  # checked first: includes a non-JSON reply
        binding, unreachable = None, True
    except ClientInputError:  # unsafe caller input is not a binding problem
        raise
    except ValueError:
        return None, "invalid binding"
    if binding is not None:
        return binding, None
    try:
        binding = load_bindings().get(intent_id)
    except ValueError:
        return None, "binding store unavailable" if unreachable else "invalid binding"
    if binding is not None:
        return binding, None
    return None, "binding store unavailable" if unreachable else "no binding for intent"


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

    # Drift check before anything is pushed: same metric set and values.
    try:
        doc = client.get_intent(intent_id)
        ok = _bounds_match(doc, binding)
    except ClientInputError:
        return rejected("invalid binding")
    except Exception:
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

    with _STATE_LOCK:
        regress = [u for u in uris if (m := _MARKS.get((intent_id, u))) is not None
                   and not (envs[u].observed_at > m[0]
                            or (envs[u].observed_at == m[0] and envs[u].value == m[1]))]
    if regress:
        return done(Outcome.INCONCLUSIVE,
                    [f"observation not newer than last pushed: {_short_name(u)}" for u in regress],
                    envs=all_envs)

    observed_at = {u: envs[u].observed_at for u in uris}
    try:
        pre_ids = {r.get("id") for r in client.list_reports(intent_id)}
        for u in uris:
            try:
                client.post_observation(intent_id, u, envs[u].value, mappings[u].unit,
                                        envs[u].observed_at)
            except ClientInputError:
                return rejected("invalid observation")
            with _STATE_LOCK:
                _MARKS[(intent_id, u)] = (envs[u].observed_at, envs[u].value)
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
    except Exception:  # Tmf921Error and anything unexpected: fail closed
        return done(Outcome.INCONCLUSIVE, ["intent server unavailable"], envs=all_envs)
    if report is None:
        return done(Outcome.INCONCLUSIVE, ["no new report"], envs=all_envs)

    verdict = interpret_report(report, now=now or clock(), max_age_s=DEFAULT_MAX_AGE_S,
                               observed_at=observed_at)
    outcome, reasons, per = verdict.outcome, list(verdict.reasons), list(verdict.per_interface)
    if outcome is Outcome.SUCCESS:
        # The server rewrites a stored bound when it evaluates Degraded: confirm
        # the bound still equals the pinned one before trusting a success.
        try:
            doc = client.get_intent(intent_id)
            still = _bounds_match(doc, binding)
        except Exception:
            return done(Outcome.INCONCLUSIVE, ["intent server unavailable"], envs=all_envs)
        if not still:
            reasons, changes = _goal_changes(doc, binding)
            return done(Outcome.INCONCLUSIVE, reasons, envs=all_envs, status="unknown",
                        changes=changes)
    return done(outcome, reasons, per, all_envs)


TEMPORAL_REQUIREMENT = "goals read now from the TMF921 intent (no history is kept)"


def assess_tmf921_as_of(
    intent_id: str,
    time_window: str,
    *,
    as_of: str | None,
    mode: str | None,
    client: Tmf921Client | None = None,
    join=None,
    now: datetime | None = None,
    metric_map=None,
    bindings=None,
) -> dict:
    """Temporal assess of a TMF921 intent: judged in the broker, nothing pushed.

    `as_of` ("10m") is an offset back from now; `mode` is "at" (the default: the
    last sample at that instant, judged fresh relative to it) or "within" (the
    worst case over `time_window` ending then, in the direction of each bound's
    operator from metric_map.json). `mode` without `as_of` ends now.

    The TMF921 server keeps no history of bounds, so the bounds are read now from
    the intent (after the same drift check against the pinned binding as the push
    path) and applied to the past telemetry. The server is never asked to judge a
    past instant: its reports describe only what was pushed to it.

    Read-only: no observation is pushed, no high-water mark is written and the
    stored fulfilment outcome is not updated (`last_outcome` is that stored value,
    read once). No per-intent lock is taken: the lock in assess_via_tmf921 orders
    the push/mark/report sequence, none of which happens here, and a concurrent
    push only changes which stored outcome is reported as `last_outcome`.

    The temporal TMF921 assess fails closed: unmapped metrics, a metric without an
    operator, drifted bounds, an unreachable server or binding store, and missing,
    stale, conflicting or non-finite evidence are never a success. Reasons are
    fixed strings; server text never reaches the result.
    """
    # Caller input first: a bad value is a client error before any I/O.
    _check_intent_id(intent_id)
    if not isinstance(time_window, str) or not _SAFE_WINDOW.match(time_window):
        raise ClientInputError(f"Unsafe time_window: {time_window!r}")
    if mode not in (None, "at", "within"):
        raise ClientInputError(f"Unknown mode: {mode!r}")
    offset_s = window_seconds(as_of, "as_of") if as_of is not None else 0
    mode = mode or "at"
    now = now or _utcnow()
    at = now - timedelta(seconds=offset_s)
    join = join or join_observed_metric
    prev = TMF921_STORE.last_outcome(intent_id)
    previous = prev.value if prev else None

    def result(outcome, reasons, per=None, envs=(), status="active", changes=()):
        out = _result(outcome, reasons, per, envs, previous, status, changes)
        out.update(jsonable_encoder({"mode": mode, "as_of": as_of, "evaluated_at": at,
                                     "requirement": TEMPORAL_REQUIREMENT}))
        return out

    binding, reason = _binding_for(intent_id, bindings)
    if binding is None:
        return result(Outcome.INCONCLUSIVE, [reason], status="unknown")
    try:
        metric_map = load_metric_map() if metric_map is None else metric_map
    except ValueError:
        return result(Outcome.INCONCLUSIVE, ["invalid binding"], status="unknown")
    client = client or Tmf921Client()
    try:
        doc = client.get_intent(intent_id)
        ok = _bounds_match(doc, binding)
    except ClientInputError:
        return result(Outcome.INCONCLUSIVE, ["invalid binding"], status="unknown")
    except Exception:  # Tmf921Error and anything unexpected: no server text
        return result(Outcome.INCONCLUSIVE, ["intent server unavailable"])
    if not ok:
        reasons, changes = _goal_changes(doc, binding)
        return result(Outcome.INCONCLUSIVE, reasons, status="unknown", changes=changes)

    life = doc.get("lifecycleStatus") if isinstance(doc, dict) else None
    if life != "ACTIVE":  # before any telemetry is read; server text is never echoed
        known = life if life in ("ACKNOWLEDGED", "SUSPENDED", "TERMINATED") else "unknown"
        return result(Outcome.INCONCLUSIVE, ["intent not active"], status=known)

    per, reasons, envs, outcomes = [], [], [], set()
    for uri, bound in binding.bounds.items():
        name = _short_name(uri)
        device, interface = binding.sources.get(uri, (None, None))
        mapping = metric_map.get(uri)
        if mapping is None or device is None:
            problem = f"unmapped metric: {name}"
        elif mapping.operator not in OPERATORS:
            problem = f"no operator for metric: {name}"
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
