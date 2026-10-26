"""Deterministic assessment: resolve intent, join telemetry, evaluate, prune context."""
from fastapi.encoders import jsonable_encoder

from broker_engine.pruner import prune
from broker_engine.sparql_resolver import resolve_intent
from broker_engine.tsdb_joiner import join_observed_metric
from outcome_assurance.evaluate import evaluate_intent

DEFAULT_MAX_ENTITIES = 50


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
    resolve = resolve or resolve_intent
    join = join or join_observed_metric
    intent = resolve(intent_id)
    interfaces = intent["interfaces"]
    observations = {}
    for iface in interfaces:
        key = (iface["device"], iface["interface_name"])
        observations[key] = join(
            iface["device"], iface["interface_name"], intent["metric"], time_window
        )
    verdict = evaluate_intent(intent, observations)  # never depends on pruning
    pruned = prune(list(observations.values()), max(max_entities, len(interfaces)))
    return jsonable_encoder({
        "verdict": verdict.outcome.value,
        "per_interface": verdict.per_interface,
        "reasons": verdict.reasons,
        "context": [e.to_dict() for e in pruned.kept],
        "dropped_count": pruned.dropped_count,
    })
