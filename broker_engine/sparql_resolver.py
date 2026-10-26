"""Resolve declared (intended) state from the Fuseki knowledge graph."""
import requests

from broker_engine.validation import REQUEST_TIMEOUT_S, IntentNotFound, _validate_token

FUSEKI_QUERY_ENDPOINT = "http://localhost:3030/network-dataset/query"


def _build_intent_sparql(intent_id: str) -> str:
    _validate_token(intent_id, "intent_id")
    return f"""
    PREFIX : <http://example.org/network-ontology#>
    SELECT ?metric ?operator ?targetValue ?device ?interfaceName WHERE {{
      :{intent_id} :hasTargetMetric ?metric ;
                    :hasOperator ?operator ;
                    :hasTargetValue ?targetValue ;
                    :appliesToLink ?link .
      ?link :hasInterface ?iface .
      ?iface :onDevice ?device ;
             :interfaceName ?interfaceName .
    }}
    """


def resolve_intent(
    intent_id: str, query_endpoint: str = FUSEKI_QUERY_ENDPOINT
) -> dict:
    sparql = _build_intent_sparql(intent_id)

    response = requests.post(
        query_endpoint,
        data={"query": sparql},
        headers={"Accept": "application/sparql-results+json"},
        timeout=REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    bindings = response.json()["results"]["bindings"]

    if not bindings:
        raise IntentNotFound(f"No such intent: {intent_id!r}")

    first = bindings[0]
    return {
        "state": "intended",
        "source": "fuseki",
        "metric": first["metric"]["value"],
        "operator": first["operator"]["value"],
        "target_value": float(first["targetValue"]["value"]),
        "interfaces": [
            {
                "device": row["device"]["value"],
                "interface_name": row["interfaceName"]["value"],
            }
            for row in bindings
        ],
    }
