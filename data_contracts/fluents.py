"""Goal, placement and link fluents: the shared, I/O-free bitemporal model.

A goal, a placement or a topology link is stored as a version node (a fluent)
that holds its value and four timestamps in the broker's own vocabulary
(`http://example.org/network-ontology#`):

    :validStart / :validEnd        valid time: when the fact held in the network
    :recordedAt / :supersededAt    knowledge time: when the broker held the record

Both intervals are half-open: a fluent is in force at valid time V and knowledge
time K when `validStart <= V < validEnd` and `recordedAt <= K < supersededAt`.
An absent end is open. With K "latest" (None) the test is only "not superseded".

Writes only ever add triples: a new fluent, a closing :validEnd on the fluent a
replace ends, or a :supersededAt on the fluent a correction replaces. Nothing is
deleted or edited in place, so a replay adds nothing and a correction never loses
the record it corrects. One change is one guarded `INSERT ... WHERE` update
(guarded_insert), which Fuseki runs in a single write transaction.

This module has no network or file I/O. The writer in ingestion/ and the readers
in broker_engine/ share it. Timestamps are compared as values, never as text, and
a timestamp without a zone is refused.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from rdflib import RDF, XSD, Graph, Literal, URIRef

from data_contracts.metric_map import check_uri

NS = "http://example.org/network-ontology#"

_KINDS = ("goal", "place", "fluent", "goal-reissue", "place-reissue", "fluent-reissue")


def _p(name: str) -> URIRef:
    return URIRef(NS + name)


def iso_ms(dt: datetime) -> str:
    """UTC, millisecond precision, `Z` suffix. A naive datetime is a ValueError."""
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timestamp needs a zone")
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_dt(text) -> datetime:
    """An aware UTC datetime from ISO 8601 text; ValueError("invalid timestamp") otherwise."""
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise ValueError("invalid timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid timestamp")
    return parsed.astimezone(timezone.utc)


def compact(dt: datetime) -> str:
    """iso_ms without '-', ':' and '.', for use inside an IRI."""
    return iso_ms(dt).replace("-", "").replace(":", "").replace(".", "")


@dataclass(frozen=True)
class Fluent:
    iri: str
    value: object
    start: datetime
    end: datetime | None
    recorded: datetime
    superseded: datetime | None

    def covers(self, valid_at: datetime) -> bool:
        return self.start <= valid_at and (self.end is None or valid_at < self.end)

    def known(self, known_at: datetime | None) -> bool:
        """known_at None means latest: every record that has not been superseded."""
        if known_at is None:
            return self.superseded is None
        return self.recorded <= known_at and (self.superseded is None or known_at < self.superseded)


def select_in_force(fluents, valid_at: datetime, known_at: datetime | None) -> Fluent | None:
    """The single fluent known at `known_at` that covers `valid_at`, or None.

    Two distinct fluents in force at once are an ambiguous record and raise
    ValueError("duplicate binding for metric"); the same fluent listed twice counts once.
    """
    found: list[Fluent] = []
    for fluent in fluents:
        if fluent.known(known_at) and fluent.covers(valid_at) and fluent not in found:
            found.append(fluent)
    if len(found) > 1:
        raise ValueError("duplicate binding for metric")
    return found[0] if found else None


def fluent_iri(base_iri: str, kind: str, recorded: datetime) -> str:
    """`<base>-<kind>-<compact recordedAt>`, so a rerun of the same write is idempotent.

    The reissue kinds name a copy of a fluent written in the same run as its successor.
    """
    if kind not in _KINDS:
        raise ValueError("unknown fluent kind")
    return f"{check_uri(base_iri, 'fluent base iri')}-{kind}-{compact(recorded)}"


def _time(dt: datetime) -> Literal:
    return Literal(iso_ms(dt), datatype=XSD.dateTime)


def _add_times(g: Graph, node: URIRef, start, recorded, end) -> None:
    g.add((node, _p("validStart"), _time(start)))
    g.add((node, _p("recordedAt"), _time(recorded)))
    if end is not None:
        g.add((node, _p("validEnd"), _time(end)))


def add_binding(g: Graph, binding_iri: str, intent_id: str, metric_uri: str) -> URIRef:
    """The stable identity node for one (intent, metric). Literals are escaped by rdflib."""
    node = URIRef(check_uri(binding_iri, "binding iri"))
    check_uri(metric_uri)
    g.add((node, RDF.type, _p("ServiceBinding")))
    g.add((node, _p("forIntent"), Literal(str(intent_id))))
    g.add((node, _p("forMetric"), Literal(metric_uri)))
    return node


def add_goal(g: Graph, binding_iri: str, value, start: datetime, recorded: datetime,
             end: datetime | None = None, *, kind: str = "goal") -> URIRef:
    if kind not in ("goal", "goal-reissue"):
        raise ValueError("unknown fluent kind")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid pinned bound") from None
    if not math.isfinite(number):
        raise ValueError("invalid pinned bound")
    binding = URIRef(check_uri(binding_iri, "binding iri"))
    node = URIRef(fluent_iri(binding_iri, kind, recorded))
    g.add((binding, _p("hasGoal"), node))
    g.add((node, RDF.type, _p("GoalFluent")))
    g.add((node, _p("pinnedBound"), Literal(number, datatype=XSD.double)))
    _add_times(g, node, start, recorded, end)
    return node


def add_placement(g: Graph, binding_iri: str, interface_iri: str, start: datetime,
                  recorded: datetime, end: datetime | None = None, *,
                  kind: str = "place") -> URIRef:
    if kind not in ("place", "place-reissue"):
        raise ValueError("unknown fluent kind")
    binding = URIRef(check_uri(binding_iri, "binding iri"))
    interface = URIRef(check_uri(interface_iri, "interface iri"))
    node = URIRef(fluent_iri(binding_iri, kind, recorded))
    g.add((binding, _p("hasPlacement"), node))
    g.add((node, RDF.type, _p("PlacementFluent")))
    g.add((node, _p("onInterface"), interface))
    _add_times(g, node, start, recorded, end)
    return node


def add_link_fluent(g: Graph, link_iri: str, iface_a: str, iface_b: str, start: datetime,
                    recorded: datetime, end: datetime | None = None, *,
                    kind: str = "fluent") -> URIRef:
    if kind not in ("fluent", "fluent-reissue"):
        raise ValueError("unknown fluent kind")
    link = URIRef(check_uri(link_iri, "link iri"))
    first = URIRef(check_uri(iface_a, "interface iri"))
    second = URIRef(check_uri(iface_b, "interface iri"))
    node = URIRef(fluent_iri(link_iri, kind, recorded))
    g.add((node, RDF.type, _p("LinkFluent")))
    g.add((node, _p("ofLink"), link))
    g.add((node, _p("hasInterface"), first))
    g.add((node, _p("hasInterface"), second))
    _add_times(g, node, start, recorded, end)
    return node


def close(g: Graph, old_iri: str, at: datetime) -> None:
    """End the valid interval of an old fluent (a replace): adds `:validEnd`."""
    g.add((URIRef(check_uri(old_iri, "fluent iri")), _p("validEnd"), _time(at)))


def supersede(g: Graph, old_iri: str, at: datetime) -> None:
    """End the knowledge interval of an old fluent (a correction): adds `:supersededAt`."""
    g.add((URIRef(check_uri(old_iri, "fluent iri")), _p("supersededAt"), _time(at)))


def guarded_insert(g: Graph, old_fluents) -> str:
    """One SPARQL update that inserts every triple of `g` only if the guard holds.

    The guard holds when each named old fluent exists and none of them has a
    :validEnd or :supersededAt yet. Replaying the update, or naming an old fluent
    that is absent or already closed, therefore adds nothing. With no old fluents
    the WHERE is empty and the update always applies. The triples are serialised by
    rdflib as N-Triples; only validated IRIs and code-formatted timestamps reach the
    text.
    """
    if len(g) == 0:
        raise ValueError("nothing to write")
    olds = [check_uri(o, "fluent iri") for o in old_fluents]
    guard = ""
    if olds:
        exists = " ".join(f"FILTER EXISTS {{ <{o}> a ?c }}" for o in olds)
        values = " ".join(f"<{o}>" for o in olds)
        absent = (f"FILTER NOT EXISTS {{ VALUES ?o {{ {values} }} ?o ?p ?x . "
                  f"FILTER(?p IN (:validEnd, :supersededAt)) }}")
        guard = f"{exists} {absent}"
    triples = g.serialize(format="nt")
    return f"PREFIX : <{NS}>\nINSERT {{\n{triples}}} WHERE {{ {guard} }}"
