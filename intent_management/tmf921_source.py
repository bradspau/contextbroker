"""Assess an intent through the TMF921 server: bind, push, poll, interpret.

Fail closed: stale, missing, conflicted, future-dated or unmappable evidence is
never a success, and nothing is pushed unless every bound metric is pushable.
Server text never reaches the result (fixed reason strings only).

Known limitation: the server exposes no used-observation time, so pushes made
by other clients (including future-dated ones) cannot be seen. The broker only
keeps an in-memory per-(intent, metric) high-water mark of what IT pushed and
refuses to push anything older (or equal with a different value).
"""
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from fastapi.encoders import jsonable_encoder

from broker_engine.pruner import prune
from broker_engine.tsdb_joiner import join_observed_metric
from broker_engine.validation import _SAFE_WINDOW, ClientInputError
from data_contracts.envelope import DEFAULT_MAX_AGE_S
from data_contracts.intent_bindings import load_bindings
from data_contracts.metric_map import load_metric_map
from intent_management.store import IntentStore
from intent_management.tmf921_client import (
    Tmf921Client,
    _check_intent_id,
    intent_bounds,
)
from outcome_assurance.evaluate import Outcome
from outcome_assurance.tmf921_report import _short_name, interpret_report

MAX_TRACKED_OUTCOMES = 1024
FUTURE_SKEW = timedelta(seconds=5)

# Separate from the graph path's store so a graph intent never inherits these.
TMF921_STORE = IntentStore(lambda _i: {})
_STATE_LOCK = threading.Lock()
_INTENT_LOCKS: dict[str, threading.Lock] = {}
_MARKS: dict[tuple[str, str], tuple[datetime, float]] = {}
_TS = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z")

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
    frac = (m.group(2) or "")[:6].ljust(6, "0")
    try:
        return datetime.strptime(m.group(1) + "." + frac, "%Y-%m-%dT%H:%M:%S.%f").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def _result(outcome: Outcome, reasons, per_interface=None, envelopes=(), previous=None,
            status="active"):
    pruned = prune(list(envelopes), max(DEFAULT_MAX_ENTITIES, len(envelopes)))
    return jsonable_encoder({
        "verdict": outcome.value,
        "per_interface": per_interface if per_interface is not None else [
            {"condition": "intent", "outcome": outcome.value, "reason": r} for r in reasons],
        "reasons": list(reasons),
        "context": [e.to_dict() for e in pruned.kept],
        "dropped_count": pruned.dropped_count,
        "intent_status": status,
        "last_outcome": previous,
        "source": "tmf921",
    })


def _unpushable(env, ref: datetime) -> str | None:
    q = env.quality
    if q.missing or env.value is None:
        return "missing observation"
    if q.stale or env.observed_at is None:
        return "stale observation"
    if env.observed_at - ref > FUTURE_SKEW:
        return "future-dated observation"
    if q.conflicts:
        return "conflicting observation"
    return None


def _ordered_reports(reports: list[dict]) -> list[dict]:
    """Newest first by parsed creationDate; the server's own order if any
    date is unparseable (never a string comparison)."""
    dates = [_parse_ts(r.get("creationDate")) for r in reports]
    if reports and all(d is not None for d in dates):
        order = sorted(range(len(reports)), key=lambda i: dates[i], reverse=True)
        return [reports[i] for i in order]
    return list(reports)


def _is_new(report, pre_ids) -> bool:
    rid = report.get("id")
    return isinstance(rid, str) and rid not in pre_ids \
        and _parse_ts(report.get("creationDate")) is not None


def _bounds_match(doc, binding) -> bool:
    current = intent_bounds(doc)
    return bool(binding.bounds) and current.keys() == binding.bounds.keys() \
        and all(current[u] == b for u, b in binding.bounds.items())


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

    try:
        bindings = load_bindings() if bindings is None else bindings
        metric_map = load_metric_map() if metric_map is None else metric_map
    except ValueError:
        return _result(Outcome.INCONCLUSIVE, ["invalid binding"], status="unknown")
    binding = bindings.get(intent_id)
    if binding is None:
        return _result(Outcome.INCONCLUSIVE, ["no binding for intent"], status="unknown")
    client = client or Tmf921Client()
    with _intent_lock(intent_id):
        return _assess_locked(intent_id, time_window, binding, client, join, now,
                              metric_map, poll_interval_s, poll_timeout_s, sleep,
                              clock, monotonic)


def _assess_locked(intent_id, time_window, binding, client, join, now, metric_map,
                   poll_interval_s, poll_timeout_s, sleep, clock, monotonic) -> dict:
    prev = TMF921_STORE.last_outcome(intent_id)
    previous = prev.value if prev else None

    def done(outcome, reasons, per=None, envs=(), status="active"):
        _record(intent_id, outcome)
        return _result(outcome, reasons, per, envs, previous, status)

    # Drift check before anything is pushed: same metric set and values.
    try:
        ok = _bounds_match(client.get_intent(intent_id), binding)
    except ClientInputError:
        return done(Outcome.INCONCLUSIVE, ["invalid binding"], status="unknown")
    except Exception:
        return done(Outcome.INCONCLUSIVE, ["intent server unavailable"])
    if not ok:
        return done(Outcome.INCONCLUSIVE, ["intent bound drifted"], status="unknown")

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
            envs[uri] = join(device, interface, mappings[uri].broker_metric, time_window)
        except ClientInputError:
            return done(Outcome.INCONCLUSIVE, ["invalid binding"])
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
                return done(Outcome.INCONCLUSIVE, ["invalid observation"], envs=all_envs)
            with _STATE_LOCK:
                _MARKS[(intent_id, u)] = (envs[u].observed_at, envs[u].value)
        report = None
        waited = 0.0
        start = monotonic()
        while True:
            new = [r for r in _ordered_reports(client.list_reports(intent_id))
                   if _is_new(r, pre_ids)]
            if len(new) >= len(uris):
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
            still = _bounds_match(client.get_intent(intent_id), binding)
        except Exception:
            return done(Outcome.INCONCLUSIVE, ["intent server unavailable"], envs=all_envs)
        if not still:
            return done(Outcome.INCONCLUSIVE, ["intent bound drifted"], envs=all_envs,
                        status="unknown")
    return done(outcome, reasons, per, all_envs)
