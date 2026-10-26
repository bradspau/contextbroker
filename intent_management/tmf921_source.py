"""Assess an intent through the TMF921 server: bind, push, poll, interpret.

Fail closed: stale, missing, conflicted or unmappable evidence is never a
success. Server text never reaches the result (fixed reason strings only).
"""
import time
from datetime import datetime, timezone

from fastapi.encoders import jsonable_encoder

from broker_engine.pruner import prune
from broker_engine.tsdb_joiner import join_observed_metric
from broker_engine.validation import _SAFE_WINDOW, ClientInputError
from data_contracts.envelope import DEFAULT_MAX_AGE_S
from data_contracts.intent_bindings import load_bindings
from data_contracts.metric_map import load_metric_map
from intent_management.tmf921_client import (
    Tmf921Client,
    Tmf921Error,
    _check_intent_id,
    intent_bounds,
)
from outcome_assurance.evaluate import Outcome
from outcome_assurance.tmf921_report import _short_name, interpret_report

DEFAULT_MAX_ENTITIES = 50


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _store():
    from broker_engine.assess import DEFAULT_STORE  # late: patches apply
    return DEFAULT_STORE


def _result(outcome: Outcome, reasons, per_interface=None, envelopes=(), previous=None):
    pruned = prune(list(envelopes), max(DEFAULT_MAX_ENTITIES, len(envelopes)))
    return jsonable_encoder({
        "verdict": outcome.value,
        "per_interface": per_interface if per_interface is not None else [
            {"condition": "intent", "outcome": outcome.value, "reason": r} for r in reasons],
        "reasons": list(reasons),
        "context": [e.to_dict() for e in pruned.kept],
        "dropped_count": pruned.dropped_count,
        "intent_status": "active",
        "last_outcome": previous,
        "source": "tmf921",
    })


def _unpushable(env) -> str | None:
    q = env.quality
    if q.missing or env.value is None:
        return "missing observation"
    if q.stale or env.observed_at is None:
        return "stale observation"
    if q.conflicts:
        return "conflicting observation"
    return None


def _newest_first(reports: list[dict]) -> list[dict]:
    return sorted(reports, key=lambda r: str(r.get("creationDate") or ""), reverse=True)


def _ids(reports) -> set:
    return {r.get("id") for r in reports}


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
) -> dict:
    # Caller input first: a bad value is a client error before any HTTP.
    _check_intent_id(intent_id)
    if not isinstance(time_window, str) or not _SAFE_WINDOW.match(time_window):
        raise ClientInputError(f"Unsafe time_window: {time_window!r}")
    join = join or join_observed_metric
    store = _store()
    previous = store.last_outcome(intent_id)
    previous = previous.value if previous else None

    def done(outcome, reasons, per=None, envs=()):
        store.fulfilment_update(intent_id, outcome)
        return _result(outcome, reasons, per, envs, previous)

    try:
        bindings = load_bindings() if bindings is None else bindings
        metric_map = load_metric_map() if metric_map is None else metric_map
    except ValueError:
        return done(Outcome.INCONCLUSIVE, ["invalid binding"])
    binding = bindings.get(intent_id)
    if binding is None:
        return done(Outcome.INCONCLUSIVE, ["no binding for intent"])
    client = client or Tmf921Client()

    # Drift check before anything is pushed.
    try:
        current = intent_bounds(client.get_intent(intent_id))
    except ClientInputError:
        return done(Outcome.INCONCLUSIVE, ["invalid binding"])
    except Exception:
        return done(Outcome.INCONCLUSIVE, ["intent server unavailable"])
    if not binding.bounds or any(current.get(u) != b for u, b in binding.bounds.items()):
        return done(Outcome.INCONCLUSIVE, ["intent bound drifted"])

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

    problems = {}
    for uri, env in envs.items():
        why = _unpushable(env)
        if why:
            problems[uri] = why
    problem_reasons = [f"{why}: {_short_name(u)}" for u, why in problems.items()]
    pushable = [u for u in uris if u not in problems]
    all_envs = list(envs.values())
    if not pushable:
        return done(Outcome.INCONCLUSIVE, problem_reasons, envs=all_envs)

    observed_at = {u: envs[u].observed_at for u in pushable}
    try:
        pre_ids = _ids(client.list_reports(intent_id))
        for u in pushable:
            client.post_observation(intent_id, u, envs[u].value, mappings[u].unit,
                                    envs[u].observed_at)
        report = None
        waited = 0.0
        while True:
            reports = _newest_first(client.list_reports(intent_id))
            if reports and reports[0].get("id") not in pre_ids:
                report = reports[0]
                break
            if waited >= poll_timeout_s:
                break
            sleep(poll_interval_s)
            waited += poll_interval_s
    except ClientInputError:
        return done(Outcome.INCONCLUSIVE, ["invalid binding"], envs=all_envs)
    except Exception:  # Tmf921Error and anything unexpected: fail closed
        return done(Outcome.INCONCLUSIVE, ["intent server unavailable"], envs=all_envs)
    if report is None:
        return done(Outcome.INCONCLUSIVE, ["no new report"], envs=all_envs)

    verdict = interpret_report(report, now=now or clock(), max_age_s=DEFAULT_MAX_AGE_S,
                               observed_at=observed_at)
    outcome, reasons, per = verdict.outcome, list(verdict.reasons), list(verdict.per_interface)
    if problems:
        for r in problem_reasons:
            per.append({"condition": "evidence", "outcome": Outcome.INCONCLUSIVE.value, "reason": r})
        if outcome is Outcome.SUCCESS:
            outcome = Outcome.INCONCLUSIVE
        reasons += problem_reasons
    return done(outcome, reasons, per, all_envs)
