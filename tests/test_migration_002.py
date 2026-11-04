"""Migration 002 (current-state bindings and links become fluents), proved in memory.

The migration is run on an rdflib graph holding the old shape (15 bindings and a link); the
result is checked against the fluent shapes, and the broker's own resolver and neighbours
code reads it back through the in-memory Fuseki stand-in. Nothing here touches a store: the
live run is a separate step that waits for the operator's approval.
"""
import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import rdflib
from pyshacl import validate
from rdflib import RDF, XSD, Literal, URIRef
from rdflib.compare import isomorphic

from broker_engine import operations as ops
from broker_engine.binding_resolver import resolve_binding
from data_contracts.fluents import parse_dt
from tests.fluent_helpers import IFACES_TTL, JIT, LAT, NS, GraphFuseki

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "ontologies" / "migrations" / "002-bitemporal-fluents.ru"
MIGRATION_001 = ROOT / "ontologies" / "migrations" / "001-retire-operational-intent.ru"
TBOX = ROOT / "ontologies" / "tbox" / "topology.ttl"
SHAPES = sorted((ROOT / "ontologies" / "shacl").glob("*.ttl"))

BW = "http://broadband-forum.org/Intent#DownstreamBandwidthMetric"
METRICS = (LAT, JIT, BW)
INTENTS = [f"intent-{n}" for n in range(1, 6)]
N = rdflib.Namespace(NS)
LINK = N["link-router-a-router-b"]


@pytest.fixture(autouse=True)
def _fuseki_credential(monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "testpassword0")


def _interfaces_ttl():
    return IFACES_TTL + f"""
    @prefix : <{NS}> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    :if-router-a-ge000 a :Interface ; :onDevice "Router-A" ; :interfaceName "GigabitEthernet0/0/0" .
    :if-router-b-ge001 a :Interface ; :onDevice "Router-B" ; :interfaceName "GigabitEthernet0/0/1" .
    {LINK.n3()} a :TopologicalLink ; rdfs:label "Router-A <-> Router-B core link" ;
        :hasInterface :if-router-a-ge000 , :if-router-b-ge001 .
    """


PLACES = ("if-olt1-pon0", "if-olt1-pon1", "if-router-a-ge000")


def _old_store():
    """The current TBox, the interface nodes, 5 intents x 3 metrics of old-shape bindings
    (distinct pins, some not whole numbers) and the old-shape link. Also returns the pins and
    placements the bindings were written with, keyed by (intent, metric)."""
    g = rdflib.Graph()
    g.parse(TBOX, format="turtle")
    g.parse(data=_interfaces_ttl(), format="turtle")
    expected = {}
    for i, intent in enumerate(INTENTS):
        for j, metric in enumerate(METRICS):
            short = metric.rsplit("#", 1)[1]
            b = N[f"binding-{intent}-{short}"]
            pin = 10.5 + i * 7 + j * 0.25
            place = N[PLACES[(i + j) % len(PLACES)]]
            g.add((b, RDF.type, N.ServiceBinding))
            g.add((b, N.forIntent, Literal(intent)))
            g.add((b, N.forMetric, Literal(metric)))
            g.add((b, N.onInterface, place))
            g.add((b, N.pinnedBound, Literal(pin, datatype=XSD.double)))
            expected[(intent, metric)] = (pin, place)
    return g, expected


def _migrated():
    g, expected = _old_store()
    before = len(g)
    g.update(MIGRATION.read_text())
    return g, expected, before


def _instant(g):
    """The single instant every fluent starts at, as a datetime."""
    starts = {str(o) for _s, _p, o in g.triples((None, N.validStart, None))}
    assert len(starts) == 1, starts
    return parse_dt(starts.pop())


def _count(g, cls):
    return len(set(g.subjects(RDF.type, cls)))


def test_every_binding_gets_one_goal_and_one_placement_fluent():
    g, expected, _ = _migrated()
    assert len(expected) == 15
    for (intent, metric), (pin, place) in expected.items():
        b = N[f"binding-{intent}-{metric.rsplit('#', 1)[1]}"]
        goals, places = list(g.objects(b, N.hasGoal)), list(g.objects(b, N.hasPlacement))
        assert len(goals) == 1 and len(places) == 1
        assert (goals[0], RDF.type, N.GoalFluent) in g
        assert (places[0], RDF.type, N.PlacementFluent) in g
        value = g.value(goals[0], N.pinnedBound)
        assert isinstance(value.toPython(), float) and value.toPython() == pin
        assert g.value(places[0], N.onInterface) == place
        # the identity of the binding is unchanged
        assert g.value(b, N.forIntent) == Literal(intent)
        assert g.value(b, N.forMetric) == Literal(metric)


def test_the_old_predicates_are_gone_from_bindings_and_links():
    g, _expected, _ = _migrated()
    for b in g.subjects(RDF.type, N.ServiceBinding):
        assert (b, N.pinnedBound, None) not in g
        assert (b, N.onInterface, None) not in g
    for link in g.subjects(RDF.type, N.TopologicalLink):
        assert (link, N.hasInterface, None) not in g


def test_the_link_pair_moves_to_one_link_fluent():
    g, _expected, _ = _migrated()
    fluents = list(g.subjects(RDF.type, N.LinkFluent))
    assert len(fluents) == 1
    f = fluents[0]
    assert g.value(f, N.ofLink) == LINK
    assert set(g.objects(f, N.hasInterface)) == {N["if-router-a-ge000"], N["if-router-b-ge001"]}
    assert g.value(LINK, rdflib.RDFS.label) == Literal("Router-A <-> Router-B core link")


def test_all_fluents_start_and_are_recorded_at_one_zoned_instant():
    g, _expected, _ = _migrated()
    fluent_nodes = (set(g.subjects(RDF.type, N.GoalFluent)) | set(g.subjects(RDF.type, N.PlacementFluent))
                    | set(g.subjects(RDF.type, N.LinkFluent)))
    assert len(fluent_nodes) == 31
    for node in fluent_nodes:
        assert g.value(node, N.validStart) == g.value(node, N.recordedAt)
        assert (node, N.validEnd, None) not in g and (node, N.supersededAt, None) not in g
    instants = {g.value(n, N.validStart) for n in fluent_nodes}
    assert len(instants) == 1
    assert _instant(g).utcoffset() == timedelta(0)  # parse_dt refuses a naive time


def test_the_scratch_triple_is_removed():
    g, _expected, _ = _migrated()
    assert (None, N.migrationInstant, None) not in g
    assert (N["migration-002"], None, None) not in g


def test_the_triple_count_follows_the_formula():
    g, _expected, before = _migrated()
    old, _ = _old_store()
    bindings, links = _count(old, N.ServiceBinding), _count(old, N.TopologicalLink)
    assert (bindings, links) == (15, 1)
    assert len(g) == before + 8 * bindings + 4 * links


def test_nothing_else_changes():
    g, _expected, _ = _migrated()
    old, _ = _old_store()
    removed_preds = {N.pinnedBound, N.onInterface, N.hasInterface}
    kept = rdflib.Graph()
    for s, p, o in old:
        if p not in removed_preds or (s, RDF.type, N.Interface) in old:
            kept.add((s, p, o))
    for s, p, o in kept:
        assert (s, p, o) in g, (s, p, o)
    # every triple of the migrated graph is either kept or belongs to a fluent or its link
    fluent_nodes = (set(g.subjects(RDF.type, N.GoalFluent)) | set(g.subjects(RDF.type, N.PlacementFluent))
                    | set(g.subjects(RDF.type, N.LinkFluent)))
    for s, p, o in g:
        if (s, p, o) in kept:
            continue
        assert s in fluent_nodes or p in (N.hasGoal, N.hasPlacement), (s, p, o)


def test_a_second_run_changes_nothing():
    g, _expected, _ = _migrated()
    snapshot = rdflib.Graph()
    for t in g:
        snapshot.add(t)
    g.update(MIGRATION.read_text())
    assert len(g) == len(snapshot)
    assert isomorphic(g, snapshot)


def test_migration_001_over_the_migrated_graph_loses_nothing():
    g, _expected, _ = _migrated()
    snapshot = rdflib.Graph()
    for t in g:
        snapshot.add(t)
    g.update(MIGRATION_001.read_text())
    assert len(g) == len(snapshot)
    assert isomorphic(g, snapshot)


@pytest.mark.parametrize("shapes", SHAPES, ids=lambda p: p.name)
def test_the_migrated_graph_conforms_to_the_shapes(shapes):
    g, _expected, _ = _migrated()
    shape_graph = rdflib.Graph().parse(shapes, format="turtle")
    conforms, _g, report = validate(g, shacl_graph=shape_graph, inference="none")
    assert conforms, report


def test_the_old_shape_does_not_conform():
    # the shapes the migrated graph passes are the ones the old shape fails
    g, _expected = _old_store()
    shape_graph = rdflib.Graph()
    for s in SHAPES:
        shape_graph.parse(s, format="turtle")
    conforms, _g, _report = validate(g, shacl_graph=shape_graph, inference="none")
    assert not conforms


def _body():
    return "\n".join(l for l in MIGRATION.read_text().splitlines() if not l.lstrip().startswith("#"))


def test_the_file_has_five_operations():
    from rdflib.plugins.sparql.processor import prepareUpdate
    assert len(prepareUpdate(MIGRATION.read_text()).algebra) == 5


def test_the_file_never_clears_drops_loads_or_creates():
    body = _body()
    for word in ("CLEAR", "DROP", "LOAD", "CREATE", "COPY", "MOVE", "ADD"):
        assert not re.search(rf"\b{word}\b", body, re.I), word
    assert "migrationInstant" in body


def test_every_delete_names_only_the_old_predicates_and_the_scratch_triple():
    allowed = {"pinnedBound", "onInterface", "hasInterface", "migrationInstant", "migration-002"}
    templates = re.findall(r"\bDELETE\s*(?:WHERE\s*)?\{([^}]*)\}", _body(), re.I)
    assert len(templates) == 4
    for template in templates:
        assert set(re.findall(r":([A-Za-z][A-Za-z0-9-]*)", template)) <= allowed, template
        assert not re.search(r"\ba\b|rdf:type|rdfs:", template), template
        assert template.count(":") <= 3  # one triple pattern: at most subject and predicate named


# ---- the broker reads the same answers after the migration ---------------------------------------

def _resolve(fx, intent, when):
    with patch("broker_engine.binding_resolver.requests.post", fx.post):
        return resolve_binding(intent, valid_at=when)


def test_the_resolver_gives_the_old_pins_and_placements_after_the_migration():
    g, expected, _ = _migrated()
    fx = GraphFuseki(g)
    instant = _instant(g)
    for intent in INTENTS:
        binding = _resolve(fx, intent, instant + timedelta(seconds=1))
        assert binding.gaps == ()
        assert set(binding.bounds) == set(METRICS)
        for metric in METRICS:
            pin, place = expected[(intent, metric)]
            assert binding.bounds[metric] == pin
            device = g.value(place, N.onDevice)
            name = g.value(place, N.interfaceName)
            assert binding.sources[metric] == (str(device), str(name))


def test_a_time_before_the_migration_is_a_gap_never_todays_goal():
    g, _expected, _ = _migrated()
    fx = GraphFuseki(g)
    instant = _instant(g)
    for intent in INTENTS:
        binding = _resolve(fx, intent, instant - timedelta(seconds=1))
        assert binding.bounds == {} and binding.sources == {}
        assert set(binding.gaps) == {(m, k) for m in METRICS for k in ("goal", "placement")}


def test_neighbours_after_the_migration_and_a_gap_before_it():
    g, _expected, _ = _migrated()
    fx = GraphFuseki(g)
    instant = _instant(g)
    now = instant + timedelta(hours=1)
    with patch("broker_engine.operations.requests.post", fx.post):
        now_out = ops.interface_neighbours("Router-A", "GigabitEthernet0/0/0", now=now)
        past_out = ops.interface_neighbours("Router-A", "GigabitEthernet0/0/0", now=now, as_of="2h")
    assert [n["device"] for n in now_out["neighbours"]] == ["Router-B"]
    assert past_out["neighbours"] == []
    assert past_out["quality"]["missing"] is False
