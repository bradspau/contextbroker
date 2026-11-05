"""Resolve an intent's service binding from the broker's network graph (Fuseki).

Reads the `:ServiceBinding` nodes (one per intent and metric) and their goal and
placement fluents (data_contracts/fluents.py): version nodes that hold the goal
pinned at binding time, or the interface a metric is read from, with a valid
interval and a knowledge interval. This is the network graph, not the TMF921
server's graph.

fetch_fluents returns every fluent of the intent. resolve_binding selects, per
metric, the goal and the placement in force at a valid time (default now) as the
broker knew them at a knowledge time (default latest, meaning not superseded).

resolve_binding returns None when the graph holds no binding for the intent,
raises ValueError for a malformed or duplicate binding, and lets
requests.RequestException through when Fuseki is unreachable. Fuseki's error
text is never put into an exception message.
"""
import math
from datetime import datetime, timezone

import requests

from broker_engine.sparql_resolver import FUSEKI_QUERY_ENDPOINT, fuseki_auth
from broker_engine.validation import REQUEST_TIMEOUT_S
from data_contracts.fluents import Fluent, parse_dt, select_in_force
from data_contracts.intent_bindings import IntentBinding
from data_contracts.metric_map import _check_token, check_uri
from intent_management.tmf921_client import _check_intent_id

_KINDS = ("goal", "placement")


def _build_binding_sparql(intent_id: str) -> str:
    # Only the validated intent id (letters, digits, hyphen) enters the query.
    _check_intent_id(intent_id)
    return f"""
    PREFIX : <http://example.org/network-ontology#>
    SELECT ?metric ?kind ?f ?val ?vs ?ve ?ra ?sa ?device ?iface WHERE {{
      ?b a :ServiceBinding ;
         :forIntent "{intent_id}" ;
         :forMetric ?metric .
      OPTIONAL {{
        {{ ?b :hasGoal ?f . ?f :pinnedBound ?val . BIND("goal" AS ?kind) }}
        UNION
        {{ ?b :hasPlacement ?f . ?f :onInterface ?val .
           ?val :onDevice ?device ; :interfaceName ?iface .
           BIND("placement" AS ?kind) }}
        ?f :validStart ?vs ; :recordedAt ?ra .
        OPTIONAL {{ ?f :validEnd ?ve }}
        OPTIONAL {{ ?f :supersededAt ?sa }}
      }}
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


def _cell(row: dict, name: str) -> str | None:
    cell = row.get(name)
    return None if cell is None else cell["value"]


def _time(text: str | None) -> datetime | None:
    if text is None:
        return None
    try:
        return parse_dt(text)
    except ValueError:
        raise ValueError("malformed binding result") from None


def fetch_fluents(intent_id: str, query_endpoint: str = FUSEKI_QUERY_ENDPOINT):
    """Every goal and placement fluent of the intent: (fluents, interfaces).

    fluents maps (metric URI, "goal" | "placement") to a list of Fluent; each metric of
    a binding node is present under both kinds, with an empty list when it has no
    fluent. A placement fluent's value is its interface IRI; interfaces maps that IRI to
    (device, interface name). No binding node at all gives ({}, {}).
    """
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
        parsed = [{name: _cell(row, name) for name in
                   ("metric", "kind", "f", "val", "vs", "ve", "ra", "sa", "device", "iface")}
                  for row in rows]
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("malformed binding result") from exc

    fluents: dict[tuple[str, str], list[Fluent]] = {}
    interfaces: dict[str, tuple[str, str]] = {}
    for row in parsed:
        metric = check_uri(row["metric"])
        for kind in _KINDS:
            fluents.setdefault((metric, kind), [])
        if row["f"] is None:
            continue  # a binding node that has no fluent yet
        kind = row["kind"]
        if kind not in _KINDS or None in (row["val"], row["vs"], row["ra"]):
            raise ValueError("malformed binding result")
        iri = check_uri(row["f"], "fluent iri")
        if kind == "goal":
            value = _pin(row["val"])
        else:
            value = check_uri(row["val"], "interface iri")
            if row["device"] is None or row["iface"] is None:
                raise ValueError("malformed binding result")
            where = (_check_token(row["device"], "device"), _check_token(row["iface"], "interface"))
            if interfaces.setdefault(value, where) != where:
                raise ValueError("duplicate binding for metric")
        start, recorded = _time(row["vs"]), _time(row["ra"])
        fluent = Fluent(iri, value, start, _time(row["ve"]), recorded, _time(row["sa"]))
        # Identical rows are SPARQL join artefacts; select_in_force rejects distinct overlaps.
        if fluent not in fluents[(metric, kind)]:
            fluents[(metric, kind)].append(fluent)
    return fluents, interfaces


def _aware(value: datetime | None, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} needs an aware datetime")
    return value.astimezone(timezone.utc)


def select_binding(
    intent_id: str,
    fluents: dict,
    interfaces: dict,
    *,
    valid_at: datetime,
    known_at: datetime | None,
) -> IntentBinding:
    """The selection half of resolve_binding, run on a snapshot from fetch_fluents.

    A caller that needs both the fluent list (eligibility) and the binding (the verdict)
    reads Fuseki once and passes the same snapshot to both. Pure: no I/O, no clock.
    `valid_at` and `known_at` must already be aware datetimes (known_at None means latest).
    Two distinct fluents in force at once raise ValueError("duplicate binding for metric").
    """
    sources: dict[str, tuple[str, str]] = {}
    bounds: dict[str, float] = {}
    goal_intervals: dict[str, tuple[datetime, datetime | None]] = {}
    placement_intervals: dict[str, tuple[datetime, datetime | None]] = {}
    gaps: list[tuple[str, str]] = []
    for metric in sorted({m for m, _kind in fluents}):
        goal = select_in_force(fluents[(metric, "goal")], valid_at, known_at)
        place = select_in_force(fluents[(metric, "placement")], valid_at, known_at)
        if goal is None:
            gaps.append((metric, "goal"))
        if place is None:
            gaps.append((metric, "placement"))
        if goal is None or place is None:
            continue  # a metric is judged only with both; the assess reports the gap
        sources[metric] = interfaces[place.value]
        bounds[metric] = goal.value
        goal_intervals[metric] = (goal.start, goal.end)
        placement_intervals[metric] = (place.start, place.end)
    return IntentBinding(intent_id, sources, bounds, goal_intervals, placement_intervals,
                         tuple(gaps))


def resolve_binding(
    intent_id: str,
    query_endpoint: str = FUSEKI_QUERY_ENDPOINT,
    *,
    valid_at: datetime | None = None,
    known_at: datetime | None = None,
) -> IntentBinding | None:
    """The binding in force at `valid_at` (default now) as known at `known_at`.

    known_at None means the latest record: fluents that have not been superseded. A
    metric with no goal or no placement in force is not an error: it is listed in
    `gaps` (sorted by metric URI, then "goal" before "placement") and left out of
    `sources` and `bounds`, so the caller can name it and refuse partial coverage.
    """
    valid_at = _aware(valid_at, "valid_at") or datetime.now(timezone.utc)
    known_at = _aware(known_at, "known_at")
    fluents, interfaces = fetch_fluents(intent_id, query_endpoint)
    if not fluents:
        return None
    return select_binding(intent_id, fluents, interfaces, valid_at=valid_at, known_at=known_at)
