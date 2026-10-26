"""Deterministic assessment: resolve intent, join telemetry, evaluate, prune context."""
from datetime import datetime, timezone

from fastapi.encoders import jsonable_encoder

from broker_engine.pruner import prune
from broker_engine.sparql_resolver import resolve_intent
from broker_engine.tsdb_joiner import join_observed_metric
from broker_engine.validation import _SAFE_WINDOW, ClientInputError, _validate_token
from data_contracts.envelope import build_observed
from intent_management.store import IntentStore
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
) -> dict:
    """Resolve the intent, join telemetry, evaluate on the full observations,
    then prune only the context returned. `resolve`/`join` default to the real
    resolver/joiner (looked up at call time so patches apply)."""
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
    for iface in interfaces:
        key = (iface["device"], iface["interface_name"])
        try:
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
    store.fulfilment_update(intent_id, verdict.outcome)
    pruned = prune(list(observations.values()), max(max_entities, len(interfaces)))
    return jsonable_encoder({
        "verdict": verdict.outcome.value,
        "per_interface": verdict.per_interface,
        "reasons": verdict.reasons,
        "context": [e.to_dict() for e in pruned.kept],
        "dropped_count": pruned.dropped_count,
        "intent_status": record.status,
        "last_outcome": previous,
    })
