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


def build(fx, device="Router-A", interface=None, **kw):
    args = dict(max_depth=3, relationship_types=BOTH, valid_at=VALID, known_at=None,
                retrieved_at=RETRIEVED, allowed=lambda d: True, query_endpoint=QUERY_URL)
    args.update(kw)
    with patch("broker_engine.context_graph.requests.post", fx.post):
        return cg.build_graph(device, interface, **args)


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
