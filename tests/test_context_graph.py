"""The Context Graph: the declared topology as nodes and edges at a valid and knowledge time.

The tracked demo topology (ontologies/abox/topology-demo.ttl) is the shared fixture; it is parsed
from the repository, never copied. Every other fixture is built in the test with rdflib and
data_contracts.fluents.add_link_fluent. Only requests.post is replaced, by the rdflib-backed
stand-in tests.fluent_helpers.GraphFuseki, so the real SPARQL readers run end to end.
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import rdflib

from broker_engine import context_graph as cg
from data_contracts import fluents as f
from data_contracts.context_contract import MAX_DEPTH, NODE_CAP
from tests.fluent_helpers import IFACES_TTL, NS, QUERY_URL, T0, T1, GraphFuseki

DEMO = Path(__file__).resolve().parent.parent / "ontologies" / "abox" / "topology-demo.ttl"
VALID = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
BEFORE = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
RETRIEVED = datetime(2026, 11, 6, 12, 0, 5, tzinfo=timezone.utc)
BOTH = ["has_interface", "connected_to"]
HOUR = timedelta(hours=1)

A_IF = "Router-A/GigabitEthernet0/0/0"
B_IF = "Router-B/GigabitEthernet0/0/1"
LINK = "link-router-a-router-b"
FLUENT = "link-router-a-router-b-fluent-demo"


@pytest.fixture(autouse=True)
def _credential(monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "testpassword0")


def demo():
    g = rdflib.Graph()
    g.parse(DEMO, format="turtle")
    return GraphFuseki(g)


TOKEN = re.compile(r"^[A-Za-z0-9_\-/]+\Z")
URI = re.compile(r"^https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+\Z")


def check_bounded(sent, max_depth):
    """At most two queries per level and max_depth + 1 levels; only validated values in VALUES."""
    assert len(sent) <= 2 * (max_depth + 1)
    for text in sent:
        for block in re.findall(r"VALUES \?\w+ \{ (.*?) \}", text):
            for item in re.findall(r'"[^"]*"|<[^>]*>', block):
                if item.startswith('"'):
                    assert TOKEN.match(item[1:-1])
                else:
                    assert URI.match(item[1:-1])


def build(fx, device="Router-A", interface=None, **kw):
    args = dict(max_depth=3, relationship_types=BOTH, valid_at=VALID, known_at=None,
                retrieved_at=RETRIEVED, allowed=lambda d: True, query_endpoint=QUERY_URL)
    args.update(kw)
    before = len(queries(fx))
    with patch("broker_engine.context_graph.requests.post", fx.post):
        got = cg.build_graph(device, interface, **args)
    check_bounded(queries(fx)[before:], args["max_depth"])
    return got


def ids(result):
    return [n.id for n in result.graph.nodes]


def edge_keys(result):
    return [(e.type, e.from_, e.to) for e in result.graph.edges]


def queries(fx):
    return [kw["data"]["query"] for url, kw in fx.posts if url.endswith("/query")]


# ---- Task 1: the tracer on the tracked demo topology ------------------------------------


def test_the_demo_topology_is_walkable_from_router_a_to_router_b():
    fx = demo()
    got = build(fx)
    assert [(n.id, n.type, n.device, n.interface, n.graph_ref, n.depth) for n in got.graph.nodes] == [
        ("Router-A", "device", "Router-A", None, None, 0),
        (A_IF, "interface", "Router-A", "GigabitEthernet0/0/0", "if-router-a-ge000", 1),
        (B_IF, "interface", "Router-B", "GigabitEthernet0/0/1", "if-router-b-ge001", 2),
        ("Router-B", "device", "Router-B", None, None, 3),
    ]
    assert [(e.type, e.from_, e.to, e.link, e.fluent) for e in got.graph.edges] == [
        ("connected_to", A_IF, B_IF, LINK, FLUENT),
        ("has_interface", "Router-A", A_IF, None, None),
        ("has_interface", "Router-B", B_IF, None, None),
    ]
    limits = got.graph.limits
    assert (limits.requested_depth, limits.max_depth, limits.node_cap) == (3, MAX_DEPTH, NODE_CAP)
    assert limits.relationship_types == ["connected_to", "has_interface"]
    assert limits.cut_by == []
    assert got.scope_identity == "resolved"
    assert (got.conflicting_links, got.colliding_ids, got.malformed) == (False, False, False)


def test_the_link_is_one_fluent_value_with_fuseki_provenance():
    got = build(demo())
    assert len(got.fluent_values) == 1
    value = got.fluent_values[0]
    assert value.context_class == "topology" and value.kind == "link"
    assert value.subject == LINK
    assert value.value == [A_IF, B_IF]
    assert value.in_force is True
    assert value.valid_from == datetime(2026, 11, 5, tzinfo=timezone.utc).isoformat()
    assert value.valid_to is None and value.superseded_at is None
    assert value.recorded_at == datetime(2026, 11, 5, tzinfo=timezone.utc).isoformat()
    assert value.provenance.source_system == "fuseki"
    assert value.provenance.reference == NS + FLUENT
    assert value.provenance.observed_at == value.recorded_at
    assert value.provenance.retrieved_at == RETRIEVED.isoformat()


def test_depth_one_stops_at_the_interface_and_names_depth():
    got = build(demo(), max_depth=1)
    assert ids(got) == ["Router-A", A_IF]
    assert edge_keys(got) == [("has_interface", "Router-A", A_IF)]
    assert got.graph.limits.cut_by == ["depth"]
    assert got.graph.limits.requested_depth == 1
    assert got.fluent_values == []


def test_depth_zero_is_the_scope_alone_and_names_depth():
    got = build(demo(), max_depth=0)
    assert ids(got) == ["Router-A"] and got.graph.edges == []
    assert got.graph.limits.cut_by == ["depth"]
    assert got.scope_identity == "resolved"


def test_an_interface_scope_has_its_device_and_peer_one_hop_away():
    got = build(demo(), "Router-A", "GigabitEthernet0/0/0", max_depth=2)
    assert [(n.id, n.depth) for n in got.graph.nodes] == [
        (A_IF, 0), ("Router-A", 1), (B_IF, 1), ("Router-B", 2)]
    assert got.graph.limits.cut_by == []
    assert got.scope_identity == "resolved"
    assert len(got.fluent_values) == 1


def test_a_valid_time_before_the_link_fluent_has_no_connected_to_edge():
    got = build(demo(), valid_at=BEFORE)
    assert ids(got) == ["Router-A", A_IF]
    assert edge_keys(got) == [("has_interface", "Router-A", A_IF)]
    assert got.fluent_values == []
    assert got.graph.limits.cut_by == []
    assert got.scope_identity == "resolved"


def test_the_graph_serialises_with_from_keys_and_no_json_ld_keys():
    text = build(demo()).graph.model_dump_json(by_alias=True)
    assert '"from"' in text and '"from_"' not in text
    for key in ("@context", "@id", "@type"):
        assert key not in text


def test_reading_goes_through_the_sparql_readers_and_never_changes_interface_neighbours():
    fx = demo()
    build(fx)
    sent = queries(fx)
    assert sent and all("LinkFluent" in q or "Interface" in q for q in sent)
    assert not fx.updates()


# ---- pure helpers -----------------------------------------------------------------------


@pytest.mark.parametrize("iri, expected", [
    (NS + "if-router-a-ge000", "if-router-a-ge000"),
    ("http://example.org/links/l1", "l1"),
    ("http://example.org/a#b", "b"),
])
def test_local_name_is_the_part_after_hash_or_the_last_slash(iri, expected):
    assert cg.local_name(iri) == expected


def row(**kw):
    base = dict(fluent=NS + "lf1", link=NS + "l1", self=NS + "if-a", self_device="a", self_name="g0",
                other=NS + "if-b", other_device="b", other_name="g1",
                start=T0, end=None, recorded=T0, superseded=None)
    base.update(kw)
    return base


def test_select_links_keeps_the_fluent_in_force_at_the_valid_and_knowledge_time():
    rows = [row(end=T1), row(fluent=NS + "lf2", start=T1, recorded=T1)]
    assert [s.fluent.iri for s in cg.select_links(rows, T0 + HOUR, None).links] == [NS + "lf1"]
    assert [s.fluent.iri for s in cg.select_links(rows, T1 + HOUR, None).links] == [NS + "lf2"]
    assert cg.select_links(rows, T0 + HOUR, T0 - HOUR).links == ()


def test_select_links_names_the_two_ends_in_sorted_node_id_order():
    sel = cg.select_links([row(self=NS + "if-z", self_device="z", other=NS + "if-b")], T0 + HOUR, None)
    assert [e.node_id for e in sel.links[0].ends] == ["b/g1", "z/g0"]
    assert sel.conflicting is False


# ---- Task 2: fixtures built in the test ---------------------------------------------------


def iface(device, name):
    return f':if-{device}-{name} a :Interface ; :onDevice "{device}" ; :interfaceName "{name}" .\n'


def iri(device, name):
    return f"{NS}if-{device}-{name}"


def topo(interfaces, links=(), extra=""):
    """A GraphFuseki holding the given (device, name) interfaces and link fluents.

    A link is (link name, (device, name), (device, name), start, recorded[, end])."""
    g = rdflib.Graph()
    g.parse(data=f"@prefix : <{NS}> .\n" + "".join(iface(d, n) for d, n in interfaces) + extra,
            format="turtle")
    for name, one, two, start, recorded, *end in links:
        f.add_link_fluent(g, f"{NS}{name}", iri(*one), iri(*two), start, recorded, *end)
    return GraphFuseki(g)


CHAIN = [("a", "g0"), ("b", "g1"), ("b", "g3"), ("c", "g2")]
CHAIN_LINKS = [("link-ab", ("a", "g0"), ("b", "g1"), T0, T0),
               ("link-bc", ("b", "g3"), ("c", "g2"), T0, T0)]
NOW = T1 + 2 * HOUR


# ---- relationship types, authority, node cap and depth name themselves ---------------------


def test_relationship_types_has_interface_only_cuts_the_link_and_names_it():
    got = build(demo(), relationship_types=["has_interface"])
    assert ids(got) == ["Router-A", A_IF]
    assert got.graph.limits.cut_by == ["relationship_types"]
    assert got.graph.limits.relationship_types == ["has_interface"]
    assert got.fluent_values == []


def test_relationship_types_connected_to_only_leaves_the_scope_alone_and_names_it():
    got = build(demo(), relationship_types=["connected_to"])
    assert ids(got) == ["Router-A"] and got.graph.edges == []
    assert got.graph.limits.cut_by == ["relationship_types"]


def test_relationship_types_connected_to_only_from_an_interface_has_the_two_interfaces():
    got = build(demo(), "Router-A", "GigabitEthernet0/0/0", relationship_types=["connected_to"])
    assert ids(got) == [A_IF, B_IF]
    assert edge_keys(got) == [("connected_to", A_IF, B_IF)]
    assert got.graph.limits.cut_by == ["relationship_types"]


def test_an_empty_relationship_type_list_follows_nothing():
    got = build(demo(), relationship_types=[])
    assert ids(got) == ["Router-A"]
    assert got.graph.limits.cut_by == ["relationship_types"]


def test_authority_drops_a_discovered_device_and_names_it():
    got = build(demo(), allowed=lambda d: d == "Router-A")
    assert ids(got) == ["Router-A", A_IF]
    assert edge_keys(got) == [("has_interface", "Router-A", A_IF)]
    assert got.fluent_values == []
    assert got.graph.limits.cut_by == ["authority"]


def test_authority_at_depth_one_cuts_nothing_the_caller_may_see():
    got = build(demo(), max_depth=1, allowed=lambda d: d == "Router-A")
    assert ids(got) == ["Router-A", A_IF]
    assert got.graph.limits.cut_by == []


def test_authority_never_expands_the_interfaces_of_a_device_it_dropped():
    fx = topo(CHAIN, CHAIN_LINKS)
    got = build(fx, "b", valid_at=NOW, allowed=lambda d: d != "c")
    assert ids(got) == ["b", "b/g1", "b/g3", "a/g0", "a"]
    assert not any(n.device == "c" for n in got.graph.nodes)
    assert all("c" not in (e.from_, e.to) and not e.to.startswith("c/") for e in got.graph.edges)
    assert [v.subject for v in got.fluent_values] == ["link-ab"]
    assert got.graph.limits.cut_by == ["authority"]
    sent = " ".join(queries(fx))
    assert iri("c", "g2") not in sent and '"c"' not in sent


def test_authority_is_not_reported_when_nothing_was_dropped():
    got = build(topo(CHAIN, CHAIN_LINKS), "b", valid_at=NOW, allowed=lambda d: d in "abc")
    assert got.graph.limits.cut_by == [] and "c/g2" in ids(got)


def star(count=60, links=False):
    interfaces = [("hub", f"if-{i:02d}") for i in range(count)]
    pairs = []
    if links:
        interfaces += [(f"peer{i:02d}", "e0") for i in range(count)]
        pairs = [(f"link-{i:02d}", ("hub", f"if-{i:02d}"), (f"peer{i:02d}", "e0"), T0, T0)
                 for i in range(count)]
    return topo(interfaces, pairs)


def test_node_cap_keeps_the_scope_then_the_lowest_ids_and_names_it():
    got = build(star(), "hub", max_depth=1, valid_at=NOW, node_cap=50)
    wanted = sorted(f"hub/if-{i:02d}" for i in range(60))[:49]
    assert ids(got) == ["hub"] + wanted
    assert len(got.graph.nodes) == 50
    assert got.graph.limits.cut_by == ["node_cap"]
    assert got.graph.limits.node_cap == 50


def test_node_cap_keeps_a_priority_node_before_the_others():
    got = build(star(), "hub", max_depth=1, valid_at=NOW, node_cap=50,
                priority=frozenset({"hub/if-59"}))
    others = sorted(f"hub/if-{i:02d}" for i in range(59))[:48]
    assert ids(got) == ["hub"] + sorted(others + ["hub/if-59"])
    assert "hub/if-48" not in ids(got)
    assert got.graph.limits.cut_by == ["node_cap"]


def test_node_cap_is_not_reported_when_everything_fits():
    got = build(star(), "hub", max_depth=1, valid_at=NOW, node_cap=61)
    assert len(got.graph.nodes) == 61
    assert got.graph.limits.cut_by == []


def test_node_cap_and_depth_are_both_named_in_sorted_order():
    got = build(star(links=True), "hub", max_depth=1, valid_at=NOW, node_cap=50)
    assert len(got.graph.nodes) == 50
    assert got.graph.limits.cut_by == ["depth", "node_cap"]


def test_node_cap_drops_edges_to_nodes_it_left_out():
    got = build(star(), "hub", max_depth=1, valid_at=NOW, node_cap=3)
    assert ids(got) == ["hub", "hub/if-00", "hub/if-01"]
    assert edge_keys(got) == [("has_interface", "hub", "hub/if-00"), ("has_interface", "hub", "hub/if-01")]


def test_depth_alone_is_named_when_a_further_allowed_node_exists():
    got = build(topo(CHAIN, CHAIN_LINKS), "a", max_depth=2, valid_at=NOW)
    assert ids(got) == ["a", "a/g0", "b/g1"]
    assert got.graph.limits.cut_by == ["depth"]


def test_several_limits_are_all_listed_in_sorted_order():
    got = build(topo(CHAIN, CHAIN_LINKS), "b", max_depth=2, valid_at=NOW,
                relationship_types=["has_interface"], allowed=lambda d: d != "c")
    assert ids(got) == ["b", "b/g1", "b/g3"]
    assert got.graph.limits.cut_by == ["authority", "relationship_types"]


def test_a_type_filtered_edge_to_an_unauthorised_device_names_authority_only():
    got = build(topo(CHAIN, CHAIN_LINKS), "b", max_depth=2, valid_at=NOW,
                relationship_types=["has_interface"], allowed=lambda d: d == "b")
    assert got.graph.limits.cut_by == ["authority"]


# ---- identity, conflicts and malformed records ---------------------------------------------

TWIN = ':if-olt1-pon0-twin a :Interface ; :onDevice "olt1" ; :interfaceName "pon0" .\n'


def twin_fx():
    g = rdflib.Graph()
    g.parse(data=f"@prefix : <{NS}> .\n" + IFACES_TTL.split("\n", 2)[2] + TWIN, format="turtle")
    return GraphFuseki(g)


def test_ambiguous_interface_scope_returns_an_empty_graph():
    got = build(twin_fx(), "olt1", "pon0")
    assert got.scope_identity == "ambiguous"
    assert got.graph.nodes == [] and got.graph.edges == []
    assert got.colliding_ids is False and got.malformed is False


def test_colliding_ids_are_flagged_and_never_merged_into_two_nodes():
    got = build(twin_fx(), "olt1")
    assert got.colliding_ids is True
    assert ids(got) == ["olt1", "olt1/pon0", "olt1/pon1"]
    assert got.graph.nodes[1].graph_ref == "if-olt1-pon0"
    assert got.scope_identity == "resolved"


def test_an_unambiguous_interface_next_to_a_twin_is_still_resolved():
    got = build(twin_fx(), "olt1", "pon1")
    assert got.scope_identity == "resolved" and got.colliding_ids is True


def test_a_missing_device_or_interface_is_missing_with_an_empty_graph():
    assert build(demo(), "Nowhere").scope_identity == "missing"
    got = build(demo(), "Router-A", "no-such")
    assert got.scope_identity == "missing" and got.graph.nodes == []
    assert got.graph.limits.cut_by == []


def test_a_clean_topology_sets_no_finding():
    got = build(demo())
    assert (got.conflicting_links, got.colliding_ids, got.malformed) == (False, False, False)


def test_conflicting_links_one_link_with_two_fluents_in_force():
    links = [("link-ab", ("a", "g0"), ("b", "g1"), T0, T0), ("link-ab", ("a", "g0"), ("c", "g2"), T0, T1)]
    got = build(topo(CHAIN, links), "a", valid_at=NOW)
    assert got.conflicting_links is True


def test_conflicting_links_a_fluent_with_three_interfaces():
    fx = topo(CHAIN, [("link-ab", ("a", "g0"), ("b", "g1"), T0, T0)])
    fluent = rdflib.URIRef(f.fluent_iri(NS + "link-ab", "fluent", T0))
    fx.graph.add((fluent, rdflib.URIRef(NS + "hasInterface"), rdflib.URIRef(iri("c", "g2"))))
    got = build(fx, "a", valid_at=NOW)
    assert got.conflicting_links is True
    assert got.fluent_values == []


def test_conflicting_links_a_fluent_with_one_interface():
    fx = topo(CHAIN, [("link-ab", ("a", "g0"), ("b", "g1"), T0, T0)])
    fluent = rdflib.URIRef(f.fluent_iri(NS + "link-ab", "fluent", T0))
    fx.graph.remove((fluent, rdflib.URIRef(NS + "hasInterface"), rdflib.URIRef(iri("b", "g1"))))
    got = build(fx, "a", valid_at=NOW)
    assert got.conflicting_links is True


def test_conflicting_links_two_fluents_on_different_links_are_not_a_conflict():
    got = build(topo(CHAIN, CHAIN_LINKS), "b", valid_at=NOW)
    assert got.conflicting_links is False
    assert [v.subject for v in got.fluent_values] == ["link-ab", "link-bc"]


def test_a_successor_fluent_replaces_the_closed_one_by_valid_time():
    links = [("link-ab", ("a", "g0"), ("b", "g1"), T0, T0, T1),
             ("link-ab", ("a", "g0"), ("c", "g2"), T1, T1)]
    fx = topo(CHAIN, links)
    first = build(fx, "a", valid_at=T0 + HOUR)
    second = build(fx, "a", valid_at=T1 + HOUR)
    assert "b/g1" in ids(first) and "c/g2" not in ids(first)
    assert "c/g2" in ids(second) and "b/g1" not in ids(second)
    assert first.conflicting_links is False and second.conflicting_links is False
    assert [v.valid_to for v in first.fluent_values] == [T1.isoformat()]
    assert [v.valid_to for v in second.fluent_values] == [None]


def test_a_fluent_recorded_after_known_at_is_not_used():
    fx = topo(CHAIN, [("link-ab", ("a", "g0"), ("b", "g1"), T0, T1)])
    assert "b/g1" not in ids(build(fx, "a", valid_at=NOW, known_at=T0 + HOUR))
    assert "b/g1" in ids(build(fx, "a", valid_at=NOW, known_at=T1 + HOUR))


def test_a_superseded_fluent_is_not_used_when_known_at_is_latest():
    fx = topo(CHAIN, [("link-ab", ("a", "g0"), ("b", "g1"), T0, T0)])
    g = rdflib.Graph()
    f.supersede(g, f.fluent_iri(NS + "link-ab", "fluent", T0), T1)
    fx.graph += g
    assert "b/g1" not in ids(build(fx, "a", valid_at=NOW))
    assert "b/g1" in ids(build(fx, "a", valid_at=NOW, known_at=T0 + HOUR))
    assert "b/g1" not in ids(build(fx, "a", valid_at=NOW, known_at=T1 + HOUR))


def test_malformed_device_value_gives_an_empty_graph_and_is_never_requeried():
    bad = 'bad "dev x'
    fx = topo([("a", "g0")], extra=f':if-bad a :Interface ; :onDevice """{bad}""" ; :interfaceName "g9" .\n')
    f.add_link_fluent(fx.graph, NS + "link-bad", iri("a", "g0"), NS + "if-bad", T0, T0)
    got = build(fx, "a", valid_at=NOW)
    assert got.malformed is True
    assert got.graph.nodes == [] and got.graph.edges == [] and got.fluent_values == []
    assert got.graph.limits.cut_by == []
    assert not any("bad" in q or "dev x" in q for q in queries(fx))


def test_malformed_valid_start_that_is_not_a_timestamp():
    fx = topo(CHAIN, [("link-ab", ("a", "g0"), ("b", "g1"), T0, T0)])
    fluent = rdflib.URIRef(f.fluent_iri(NS + "link-ab", "fluent", T0))
    start = rdflib.URIRef(NS + "validStart")
    fx.graph.remove((fluent, start, None))
    fx.graph.add((fluent, start, rdflib.Literal("yesterday")))
    got = build(fx, "a", valid_at=NOW)
    assert got.malformed is True and got.graph.nodes == []


def test_malformed_unsafe_iri_read_back_is_refused():
    fx = topo(CHAIN, [])
    fx.graph.add((rdflib.URIRef("urn:x:not-http"), rdflib.RDF.type, rdflib.URIRef(NS + "Interface")))
    fx.graph.add((rdflib.URIRef("urn:x:not-http"), rdflib.URIRef(NS + "onDevice"), rdflib.Literal("a")))
    fx.graph.add((rdflib.URIRef("urn:x:not-http"), rdflib.URIRef(NS + "interfaceName"), rdflib.Literal("g7")))
    got = build(fx, "a", valid_at=NOW)
    assert got.malformed is True and got.graph.nodes == []


# ---- bounds ---------------------------------------------------------------------------------


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
@pytest.mark.parametrize("scope", [("Router-A", None), ("Router-A", "GigabitEthernet0/0/0")])
def test_the_number_of_queries_is_bounded_by_two_per_level(depth, scope):
    fx = demo()
    build(fx, *scope, max_depth=depth)  # build() asserts the bound and the VALUES contents
    assert 1 <= len(queries(fx)) <= 2 * (depth + 1)


def test_a_device_read_once_is_not_read_again():
    fx = demo()
    build(fx, "Router-A", "GigabitEthernet0/0/0", max_depth=3)
    assert sum('VALUES ?d { "Router-A" }' in q for q in queries(fx)) == 1


@pytest.mark.parametrize("kw", [
    {"max_depth": 4}, {"max_depth": -1}, {"max_depth": True}, {"node_cap": 0},
    {"relationship_types": ["follows"]}, {"relationship_types": ["connected_to", "connected_to"]},
    {"valid_at": datetime(2026, 11, 6, 12, 0)},
])
def test_unusable_arguments_are_refused_before_any_query(kw):
    fx = demo()
    with pytest.raises(ValueError):
        build(fx, **kw)
    assert queries(fx) == []


@pytest.mark.parametrize("device, interface", [('a"b', None), ("a b", None), ("Router-A", 'x"y'), ("", None)])
def test_an_unsafe_scope_never_reaches_a_query(device, interface):
    fx = demo()
    with pytest.raises(ValueError):
        build(fx, device, interface)
    assert queries(fx) == []


# ---- read_device_names (Phase 5, D-07) ----------------------------------------------------


def _devices(fx):
    with patch("broker_engine.context_graph.requests.post", fx.post):
        return cg.read_device_names(QUERY_URL)


def test_read_device_names_is_sorted_and_distinct():
    assert _devices(demo()) == ["Router-A", "Router-B"]
    g = rdflib.Graph()
    g.parse(data=IFACES_TTL, format="turtle")
    assert _devices(GraphFuseki(g)) == ["olt1"]
    assert _devices(GraphFuseki(rdflib.Graph())) == []


def test_read_device_names_refuses_a_bad_value_without_echoing_it():
    g = rdflib.Graph()
    g.parse(data=f'@prefix : <{NS}> . :i a :Interface ; :onDevice "Router A canary77" .',
            format="turtle")
    with pytest.raises(cg.MalformedTopology) as exc:
        _devices(GraphFuseki(g))
    assert "canary77" not in str(exc.value)


def test_read_device_names_refuses_a_non_json_reply_and_lets_a_network_error_through():
    from unittest.mock import Mock
    bad = Mock()
    bad.raise_for_status.return_value = None
    bad.json.side_effect = ValueError("not json")
    with patch("broker_engine.context_graph.requests.post", return_value=bad):
        with pytest.raises(cg.MalformedTopology):
            cg.read_device_names(QUERY_URL)
    import requests
    with patch("broker_engine.context_graph.requests.post",
               side_effect=requests.ConnectionError("down")):
        with pytest.raises(requests.ConnectionError):
            cg.read_device_names(QUERY_URL)
