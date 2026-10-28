"""Deterministic assessment: resolve intent, join telemetry, evaluate, prune context."""
from datetime import datetime, timedelta, timezone

from fastapi.encoders import jsonable_encoder

from broker_engine.pruner import prune
from broker_engine.sparql_resolver import resolve_intent
from broker_engine.temporal import temporal_observation
from broker_engine.tsdb_joiner import join_observed_metric
from broker_engine.validation import _SAFE_WINDOW, ClientInputError, _validate_token, window_seconds
from data_contracts.envelope import build_observed
from intent_management.store import IntentStore
from intent_management.tmf921_source import assess_tmf921_as_of, assess_via_tmf921
from outcome_assurance.evaluate import Outcome, evaluate_intent

DEFAULT_MAX_ENTITIES = 50

# Late-bound resolver so patches of broker_engine.assess.resolve_intent apply.
DEFAULT_STORE = IntentStore(lambda intent_id: resolve_intent(intent_id))


def assess_intent(
    intent_id: str,
    time_window: str,
    max_entities: int = DEFAULT_MAX_ENTITIES,
    *,
    resolve=None,
    join=None,
    source: str = "graph",
    as_of: str | None = None,
    mode: str | None = None,
) -> dict:
    """Resolve the intent, join telemetry, evaluate on the full observations,
    then prune only the context returned. `resolve`/`join` default to the real
    resolver/joiner (looked up at call time so patches apply).

    `source` is "graph" (default; the requirement in the broker's network graph
    in Fuseki) or "tmf921" (the separate TMF921 intent server and its own graph:
    its intent handling state combined with the broker's deterministic checks).
    The two graphs and their notion of "intent" are separate; see
    data_contracts/intent_bindings.py for the only link. For "tmf921", `max_entities` is ignored.
    Any other value raises ClientInputError.

    Temporal queries (both sources; with neither argument the behaviour is
    unchanged). `as_of` ("10m") evaluates as of that long ago, up to 30d.
    `mode` is "at" (default when as_of is set): the latest sample at that
    instant, which must be fresh relative to it. "within": the worst case over
    the `time_window` ending at that instant (now if no as_of), so a breach
    anywhere in it counts. The requirement is always the intent's current
    definition (neither graph keeps history), and a historical assessment never
    updates the intent's current fulfilment state. For "tmf921" the bounds are
    read now from the TMF921 intent and the broker judges the past telemetry
    itself: nothing is pushed to the server (see assess_tmf921_as_of)."""
    if source not in ("graph", "tmf921"):
        raise ClientInputError(f"Unknown source: {source!r}")
    if mode not in (None, "at", "within"):
        raise ClientInputError(f"Unknown mode: {mode!r}")
    if as_of is not None:
        window_seconds(as_of, "as_of")
    temporal = as_of is not None or mode is not None
    if source == "tmf921":
        if temporal:
            return assess_tmf921_as_of(intent_id, time_window, as_of=as_of, mode=mode,
                                       join=join)
        return assess_via_tmf921(intent_id, time_window, join=join)
    mode = (mode or "at") if temporal else None
    join = join or join_observed_metric
    # Validate caller input up front so a bad window is a client error (400),
    # not a per-interface "invalid identifier" verdict.
    if not _SAFE_WINDOW.match(time_window):
        raise ClientInputError(f"Unsafe time_window: {time_window!r}")
    store = DEFAULT_STORE.with_resolver(resolve) if resolve else DEFAULT_STORE
    record = store.get(intent_id)
    previous = record.last_outcome.value if record.last_outcome else None
    if record.status == "unknown":
        return jsonable_encoder({
            "verdict": Outcome.INCONCLUSIVE.value,
            "per_interface": [],
            "reasons": ["intent definition incomplete"],
            "context": [],
            "dropped_count": 0,
            "intent_status": record.status,
            "last_outcome": previous,
        })
    intent = record.as_resolver_dict()
    _validate_token(intent["metric"], "metric_type")
    interfaces = intent["interfaces"]
    observations = {}
    now = datetime.now(timezone.utc)
    for iface in interfaces:
        key = (iface["device"], iface["interface_name"])
        try:
            if temporal:
                observations[key] = temporal_observation(
                    join, iface["device"], iface["interface_name"], intent, time_window,
                    mode, as_of, now)
            else:
                observations[key] = join(
                    iface["device"], iface["interface_name"], intent["metric"], time_window
                )
        except ClientInputError:
            # Window and metric were validated above, so this is one graph-supplied identifier we cannot safely query must not fail
            # the whole assessment: report that interface as missing/inconclusive.
            env = build_observed(
                f'{iface["device"]}:{iface["interface_name"]}',
                None, "victoriametrics", None, datetime.now(timezone.utc),
            )
            env.quality.conflicts.append("invalid interface identifier")
            observations[key] = env
    verdict = evaluate_intent(intent, observations)  # never depends on pruning
    if not temporal:
        store.fulfilment_update(intent_id, verdict.outcome)
    pruned = prune(list(observations.values()), max(max_entities, len(interfaces)))
    out = {
        "verdict": verdict.outcome.value,
        "per_interface": verdict.per_interface,
        "reasons": verdict.reasons,
        "context": [e.to_dict() for e in pruned.kept],
        "dropped_count": pruned.dropped_count,
        "intent_status": record.status,
        "last_outcome": previous,
    }
    if temporal:
        at = now - timedelta(seconds=window_seconds(as_of, "as_of")) if as_of else now
        out.update({
            "mode": mode, "as_of": as_of, "evaluated_at": at,
            "requirement": "current definition of the intent (no history is kept)",
        })
    return jsonable_encoder(out)

