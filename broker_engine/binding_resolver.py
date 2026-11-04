"""Resolve an intent's service binding from the broker's network graph (Fuseki).

Reads the `:ServiceBinding` nodes (one per intent and metric) that map a metric
to a device/interface and the bound pinned at binding time. This is the network
graph, not the TMF921 server's graph.

resolve_binding returns None when the graph holds no binding for the intent,
raises ValueError for a malformed or duplicate binding, and lets
requests.RequestException through when Fuseki is unreachable. Fuseki's error
text is never put into an exception message.
"""
import math

import requests

from broker_engine.sparql_resolver import FUSEKI_QUERY_ENDPOINT, fuseki_auth
from broker_engine.validation import REQUEST_TIMEOUT_S
from data_contracts.intent_bindings import IntentBinding
from data_contracts.metric_map import _check_token, check_uri
from intent_management.tmf921_client import _check_intent_id


def _build_binding_sparql(intent_id: str) -> str:
    # Only the validated intent id (letters, digits, hyphen) enters the query.
    _check_intent_id(intent_id)
    return f"""
    PREFIX : <http://example.org/network-ontology#>
    SELECT ?metric ?device ?iface ?pin WHERE {{
      ?b a :ServiceBinding ;
         :forIntent "{intent_id}" ;
         :forMetric ?metric ;
         :pinnedBound ?pin ;
         :onInterface ?i .
      ?i :onDevice ?device ;
         :interfaceName ?iface .
    }}
    """


def _pin(value) -> float:
    try:
        pin = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid pinned bound") from exc
    if not math.isfinite(pin):
        raise ValueError("invalid pinned bound")
    return pin


def resolve_binding(
    intent_id: str, query_endpoint: str = FUSEKI_QUERY_ENDPOINT
) -> IntentBinding | None:
    sparql = _build_binding_sparql(intent_id)
    response = requests.post(
        query_endpoint,
        data={"query": sparql},
        headers={"Accept": "application/sparql-results+json"},
        auth=fuseki_auth(),
        timeout=REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    try:
        rows = response.json()["results"]["bindings"]
        parsed = [(row["metric"]["value"], row["device"]["value"],
                   row["iface"]["value"], row["pin"]["value"]) for row in rows]
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("malformed binding result") from exc
    if not parsed:
        return None

    sources: dict[str, tuple[str, str]] = {}
    bounds: dict[str, float] = {}
    for metric, device, iface, pin in parsed:
        uri = check_uri(metric)
        source = (_check_token(device, "device"), _check_token(iface, "interface"))
        bound = _pin(pin)
        if uri in sources:
            # Identical rows are SPARQL join artefacts; a differing one is a duplicate.
            if sources[uri] == source and bounds[uri] == bound:
                continue
            raise ValueError("duplicate binding for metric")
        sources[uri] = source
        bounds[uri] = bound
    return IntentBinding(intent_id, sources, bounds)
