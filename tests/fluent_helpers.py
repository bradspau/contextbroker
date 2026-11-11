"""An in-memory stand-in for Fuseki, shared by the fluent history tests.

Import this module only as ``tests.fluent_helpers`` (like ``tests.auth_helpers``).

GraphFuseki wraps an rdflib Graph and offers ``post`` with the signature of
``requests.post`` so a test can patch it in for the resolver, the writer and the
loader. A URL ending in /update applies the body as a SPARQL update, /query runs
``data["query"]`` and answers in the SPARQL JSON results shape (unbound variables
left out, as Fuseki does), and /data parses Turtle into the graph.

rdflib's Graph.update is not transactional on error: never use this class to test
rollback. It proves what an update does when it succeeds, and that a replay or a
stale update changes nothing.
"""
from datetime import datetime, timezone
from unittest.mock import Mock

import rdflib
from rdflib import BNode, URIRef

from data_contracts import fluents as _f
from outcome_assurance.tmf921_report import short_name

NS = "http://example.org/network-ontology#"
LAT = "http://broadband-forum.org/Intent#LatencyMetric"
JIT = "http://broadband-forum.org/Intent#JitterMetric"

IFACES_TTL = f"""
@prefix : <{NS}> .
:if-olt1-pon0 a :Interface ; :onDevice "olt1" ; :interfaceName "pon0" .
:if-olt1-pon1 a :Interface ; :onDevice "olt1" ; :interfaceName "pon1" .
"""

IF0 = NS + "if-olt1-pon0"
IF1 = NS + "if-olt1-pon1"

# Where GraphFuseki.post receives the scenario writers' updates and the resolver's queries.
UPDATE_URL = "http://fuseki.test/ds/update"
QUERY_URL = "http://fuseki.test/ds/query"

T0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)


def _term(value) -> dict:
    if isinstance(value, URIRef):
        kind = "uri"
    elif isinstance(value, BNode):
        kind = "bnode"
    else:
        kind = "literal"
    return {"type": kind, "value": str(value)}


def _response(payload=None, status=200) -> Mock:
    r = Mock()
    r.status_code = status
    r.raise_for_status.return_value = None
    r.json.return_value = payload
    return r


class GraphFuseki:
    def __init__(self, graph: rdflib.Graph | None = None):
        self.graph = graph if graph is not None else rdflib.Graph()
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, data=None, headers=None, auth=None, timeout=None, **kw):
        self.posts.append((url, {"data": data, "headers": headers, "auth": auth,
                                 "timeout": timeout, **kw}))
        if url.endswith("/update"):
            body = data.decode("utf-8") if isinstance(data, bytes) else data
            self.graph.update(body)
            return _response(status=204)
        if url.endswith("/query"):
            result = self.graph.query(data["query"])
            rows = [{str(k): _term(v) for k, v in row.asdict().items() if v is not None}
                    for row in result]
            return _response({"head": {"vars": [str(v) for v in result.vars]},
                              "results": {"bindings": rows}})
        if url.endswith("/data"):
            body = data.decode("utf-8") if isinstance(data, bytes) else data
            self.graph.parse(data=body, format="turtle")
            return _response(status=204)
        raise AssertionError(f"unexpected URL: {url}")

    def updates(self) -> list[str]:
        """The text of every update posted so far."""
        return [(kw["data"].decode("utf-8") if isinstance(kw["data"], bytes) else kw["data"])
                for url, kw in self.posts if url.endswith("/update")]


# ---- scenario writers: the real builders and the one guarded update ----------------------


def bind(fx, intent_id="i1", metric=LAT, goal=100.0, place=IF0, at=T0) -> str:
    """Write a first binding (goal and placement both starting and recorded at `at`).

    Returns the binding node IRI, which names the fluents (so two metrics get two nodes).
    """
    binding = f"{NS}binding-{intent_id}-{short_name(metric)}"
    g = rdflib.Graph()
    _f.add_binding(g, binding, intent_id, metric)
    _f.add_goal(g, binding, goal, at, at)
    _f.add_placement(g, binding, place, at, at)
    fx.post(UPDATE_URL, data=_f.guarded_insert(g, []).encode("utf-8"))
    return binding


def replace_goal(fx, binding_iri, goal, at, old_recorded) -> str:
    """The goal changed at `at`: close the goal recorded at `old_recorded`, open the new one.

    Returns the closed goal's IRI.
    """
    old = _f.fluent_iri(binding_iri, "goal", old_recorded)
    g = rdflib.Graph()
    _f.add_goal(g, binding_iri, goal, at, at)
    _f.close(g, old, at)
    fx.post(UPDATE_URL, data=_f.guarded_insert(g, [old]).encode("utf-8"))
    return old


def correct_placement(fx, binding_iri, place, at, start, old_recorded) -> str:
    """The earlier placement record was wrong: supersede it at `at` and reissue with the same
    start. Returns the superseded placement's IRI."""
    old = _f.fluent_iri(binding_iri, "place", old_recorded)
    g = rdflib.Graph()
    _f.add_placement(g, binding_iri, place, start, at)
    _f.supersede(g, old, at)
    fx.post(UPDATE_URL, data=_f.guarded_insert(g, [old]).encode("utf-8"))
    return old
