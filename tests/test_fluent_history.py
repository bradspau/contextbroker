"""Goal and placement fluents: the model, the one guarded update and the read at (V, K).

The end-to-end tests write with data_contracts.fluents.guarded_insert and read with
broker_engine.binding_resolver.resolve_binding, with only requests.post replaced by an
rdflib-backed stand-in (tests.fluent_helpers.GraphFuseki).
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import rdflib
from rdflib.plugins.sparql.processor import prepareUpdate

from broker_engine.binding_resolver import fetch_fluents, resolve_binding
from data_contracts import fluents as f
from data_contracts.fluents import Fluent, select_in_force
from ingestion.bind_intent import bind
from ingestion.load_ontologies import validate_shapes
from tests.fluent_helpers import IFACES_TTL, JIT, LAT, NS, T0, T1, T2, GraphFuseki

POST = "broker_engine.binding_resolver.requests.post"
UPDATE = "http://fuseki.test/ds/update"
QUERY = "http://fuseki.test/ds/query"
BIND = f"{NS}binding-i1-LatencyMetric"
IF0 = f"{NS}if-olt1-pon0"
IF1 = f"{NS}if-olt1-pon1"
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


# ---- time helpers ---------------------------------------------------------------


def test_iso_ms_is_utc_milliseconds_with_z():
    dt = datetime(2026, 11, 5, 10, 15, 0, 123000, tzinfo=timezone.utc)
    assert f.iso_ms(dt) == "2026-11-05T10:15:00.123Z"


def test_iso_ms_converts_other_zones_to_utc():
    plus1 = timezone(timedelta(hours=1))
    assert f.iso_ms(datetime(2026, 11, 5, 11, 15, 0, tzinfo=plus1)) == "2026-11-05T10:15:00.000Z"


def test_iso_ms_refuses_a_naive_datetime():
    with pytest.raises(ValueError):
        f.iso_ms(datetime(2026, 11, 5, 10, 15, 0))


def test_compact_has_no_separators():
    dt = datetime(2026, 11, 5, 10, 15, 0, 123000, tzinfo=timezone.utc)
    assert f.compact(dt) == "20261105T101500123Z"


@pytest.mark.parametrize("text", [
    "2026-11-05T10:15:00Z",
    "2026-11-05T10:15:00+00:00",
    "2026-11-05T11:15:00+01:00",
    "2026-11-05T10:15:00.0Z",
    "2026-11-05T10:15:00.123456789Z",
])
def test_parse_dt_gives_the_same_utc_instant(text):
    parsed = f.parse_dt(text)
    assert parsed.utcoffset() == timedelta(0)
    assert parsed.replace(microsecond=0) == datetime(2026, 11, 5, 10, 15, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("fraction", ["1", "12", "123", "1234", "12345", "123456",
                                      "1234567", "12345678", "123456789"])
def test_parse_dt_accepts_one_to_nine_fraction_digits(fraction):
    assert f.parse_dt(f"2026-11-05T10:15:00.{fraction}Z").year == 2026


@pytest.mark.parametrize("text", ["2026-11-05T10:15:00", "yesterday", "", None, 5])
def test_parse_dt_rejects_naive_and_unparsable(text):
    with pytest.raises(ValueError):
        f.parse_dt(text)


# ---- selection ------------------------------------------------------------------


def fl(iri="http://x/a", value=1.0, start=T0, end=None, recorded=T0, superseded=None):
    return Fluent(iri, value, start, end, recorded, superseded)


def test_a_fluent_is_half_open_in_valid_time():
    fluent = fl(start=T0, end=T1)
    assert fluent.covers(T0)
    assert fluent.covers(T1 - timedelta(milliseconds=1))
    assert not fluent.covers(T1)
    assert not fluent.covers(T0 - timedelta(milliseconds=1))


def test_an_open_fluent_covers_every_time_from_its_start():
    assert fl(end=None).covers(T0 + 400 * DAY)


def test_known_at_none_skips_superseded_fluents_only():
    assert fl(superseded=None).known(None)
    assert not fl(superseded=T2).known(None)


def test_known_at_k_is_half_open_in_knowledge_time():
    fluent = fl(recorded=T1, superseded=T2)
    assert not fluent.known(T1 - timedelta(milliseconds=1))
    assert fluent.known(T1)
    assert fluent.known(T2 - timedelta(milliseconds=1))
    assert not fluent.known(T2)


def test_select_returns_the_fluent_that_is_known_and_covers():
    old = fl("http://x/old", 100.0, start=T0, end=T1)
    new = fl("http://x/new", 150.0, start=T1, recorded=T1)
    assert select_in_force([old, new], T0 + DAY, None) == old
    assert select_in_force([old, new], T1 + HOUR, None) == new


def test_select_returns_none_when_nothing_covers():
    assert select_in_force([fl(start=T1)], T0, None) is None
    assert select_in_force([], T0, None) is None


def test_select_ignores_a_fluent_not_yet_known_at_k():
    new = fl("http://x/new", 150.0, start=T1, recorded=T1)
    assert select_in_force([new], T1 + HOUR, T1 - HOUR) is None


def test_select_with_latest_skips_superseded_fluents():
    old = fl("http://x/old", "pon0", superseded=T2)
    new = fl("http://x/new", "pon1", recorded=T2)
    assert select_in_force([old, new], T1, None) == new
    assert select_in_force([old, new], T1, T2 - HOUR) == old
    assert select_in_force([old, new], T1, T2) == new


def test_two_distinct_fluents_covering_the_same_point_are_a_duplicate():
    a = fl("http://x/a", 1.0)
    b = fl("http://x/b", 2.0)
    with pytest.raises(ValueError, match="duplicate binding for metric"):
        select_in_force([a, b], T0 + HOUR, None)


def test_the_same_fluent_twice_counts_once():
    a = fl("http://x/a", 1.0)
    assert select_in_force([a, fl("http://x/a", 1.0)], T0 + HOUR, None) == a


# ---- builders ---------------------------------------------------------------------


def test_fluent_iri_embeds_the_compact_recording_time():
    assert f.fluent_iri(BIND, "goal", T1) == f"{BIND}-goal-20261103T100000000Z"


@pytest.mark.parametrize("kind", ["goal", "place", "fluent", "goal-reissue", "place-reissue",
                                  "fluent-reissue"])
def test_fluent_iri_accepts_the_known_kinds(kind):
    assert f.fluent_iri(BIND, kind, T0).startswith(f"{BIND}-{kind}-")


def test_fluent_iri_refuses_other_kinds_and_unsafe_bases():
    with pytest.raises(ValueError):
        f.fluent_iri(BIND, "other", T0)
    with pytest.raises(ValueError):
        f.fluent_iri("not an iri", "goal", T0)


def test_builders_refuse_unsafe_iris():
    g = rdflib.Graph()
    with pytest.raises(ValueError):
        f.add_binding(g, "http://x/b> a <http://y", "i1", LAT)
    with pytest.raises(ValueError):
        f.add_binding(g, BIND, "i1", "not a uri")
    with pytest.raises(ValueError):
        f.add_placement(g, BIND, "http://x/i>", T0, T0)
    with pytest.raises(ValueError):
        f.close(g, "http://x/o> ", T1)
    with pytest.raises(ValueError):
        f.guarded_insert(g, ["http://x/o> "])
    assert len(g) == 0


def test_add_goal_refuses_a_non_finite_value():
    for value in (float("nan"), float("inf"), "abc"):
        with pytest.raises(ValueError):
            f.add_goal(rdflib.Graph(), BIND, value, T0, T0)


def test_add_goal_builds_the_fluent_triples():
    g = rdflib.Graph()
    node = f.add_goal(g, BIND, 100, T0, T0, end=T1)
    assert str(node) == f.fluent_iri(BIND, "goal", T0)
    one = lambda p: g.value(node, rdflib.URIRef(NS + p))  # noqa: E731
    assert one("pinnedBound").datatype == rdflib.XSD.double
    assert float(one("pinnedBound")) == 100.0
    assert f.parse_dt(str(one("validStart"))) == T0
    assert f.parse_dt(str(one("validEnd"))) == T1
    assert f.parse_dt(str(one("recordedAt"))) == T0
    assert one("supersededAt") is None
    assert (rdflib.URIRef(BIND), rdflib.URIRef(NS + "hasGoal"), node) in g


def test_add_goal_with_the_reissue_kind_uses_a_distinct_iri():
    g = rdflib.Graph()
    node = f.add_goal(g, BIND, 100, T0, T1, kind="goal-reissue")
    assert str(node) == f.fluent_iri(BIND, "goal-reissue", T1)
    with pytest.raises(ValueError):
        f.add_goal(g, BIND, 100, T0, T1, kind="place")


def test_add_link_fluent_builds_the_pair():
    g = rdflib.Graph()
    link = f"{NS}link-a-b"
    node = f.add_link_fluent(g, link, IF0, IF1, T0, T0)
    assert (node, rdflib.RDF.type, rdflib.URIRef(NS + "LinkFluent")) in g
    assert (node, rdflib.URIRef(NS + "ofLink"), rdflib.URIRef(link)) in g
    assert {str(o) for o in g.objects(node, rdflib.URIRef(NS + "hasInterface"))} == {IF0, IF1}


# ---- the guarded update, end to end ---------------------------------------------------


def fuseki() -> GraphFuseki:
    fx = GraphFuseki()
    fx.graph.parse(data=IFACES_TTL, format="turtle")
    return fx


def first_bind(fx, goal=100.0, at=T0, place=IF0, metric=LAT, bind=BIND):
    g = rdflib.Graph()
    f.add_binding(g, bind, "i1", metric)
    f.add_goal(g, bind, goal, at, at)
    f.add_placement(g, bind, place, at, at)
    text = f.guarded_insert(g, [])
    fx.post(UPDATE, data=text.encode("utf-8"))
    return text


def replace_goal(fx, goal=150.0, at=T1, old_recorded=T0):
    old = f.fluent_iri(BIND, "goal", old_recorded)
    g = rdflib.Graph()
    f.add_goal(g, BIND, goal, at, at)
    f.close(g, old, at)
    text = f.guarded_insert(g, [old])
    fx.post(UPDATE, data=text.encode("utf-8"))
    return text, old


def correct_placement(fx, place=IF1, at=T2, start=T0, old_recorded=T0):
    old = f.fluent_iri(BIND, "place", old_recorded)
    g = rdflib.Graph()
    f.add_placement(g, BIND, place, start, at)
    f.supersede(g, old, at)
    text = f.guarded_insert(g, [old])
    fx.post(UPDATE, data=text.encode("utf-8"))
    return text, old


def read(fx, **kw):
    with patch(POST, side_effect=fx.post):
        return resolve_binding("i1", QUERY, **kw)


def test_replace_closes_the_old_goal_and_opens_the_new_one():
    fx = fuseki()
    first_bind(fx)
    _text, old = replace_goal(fx)

    old_node = rdflib.URIRef(old)
    new_node = rdflib.URIRef(f.fluent_iri(BIND, "goal", T1))
    pin = rdflib.URIRef(NS + "pinnedBound")
    assert float(fx.graph.value(old_node, pin)) == 100.0  # never deleted, value kept
    end = fx.graph.value(old_node, rdflib.URIRef(NS + "validEnd"))
    start = fx.graph.value(new_node, rdflib.URIRef(NS + "validStart"))
    assert f.parse_dt(str(end)) == f.parse_dt(str(start)) == T1
    assert float(fx.graph.value(new_node, pin)) == 150.0
    assert fx.graph.value(new_node, rdflib.URIRef(NS + "validEnd")) is None


def test_the_goal_in_force_is_read_back_at_a_past_valid_time():
    fx = fuseki()
    first_bind(fx)
    replace_goal(fx)

    before = read(fx, valid_at=T0 + DAY)
    assert before.bounds == {LAT: 100.0}
    assert before.sources == {LAT: ("olt1", "pon0")}
    assert read(fx, valid_at=T1 + HOUR).bounds == {LAT: 150.0}
    assert read(fx).bounds == {LAT: 150.0}  # no time: now, latest


def test_the_boundaries_are_half_open():
    fx = fuseki()
    first_bind(fx)
    replace_goal(fx)
    assert read(fx, valid_at=T0).bounds == {LAT: 100.0}
    assert read(fx, valid_at=T1 - timedelta(milliseconds=1)).bounds == {LAT: 100.0}
    assert read(fx, valid_at=T1).bounds == {LAT: 150.0}


def test_no_binding_node_reads_as_none():
    assert read(fuseki()) is None


def test_a_valid_time_before_the_first_fluent_is_a_named_gap():
    fx = fuseki()
    first_bind(fx)
    b = read(fx, valid_at=T0 - HOUR)
    assert b.gaps == ((LAT, "goal"), (LAT, "placement"))


def test_the_old_fluent_is_known_for_a_knowledge_time_before_the_replacement():
    fx = fuseki()
    first_bind(fx)
    replace_goal(fx)
    # Known at T1 - 1h the replacement did not exist, so the closed goal does not cover T1 + 1h.
    assert read(fx, valid_at=T1 + HOUR, known_at=T1 - HOUR).gaps == ((LAT, "goal"),)
    assert read(fx, valid_at=T0 + HOUR, known_at=T1 - HOUR).bounds == {LAT: 100.0}


def test_replaying_the_replace_adds_nothing():
    fx = fuseki()
    first_bind(fx)
    text, _old = replace_goal(fx)
    before = set(fx.graph)
    fx.post(UPDATE, data=text.encode("utf-8"))
    assert set(fx.graph) == before


def test_replaying_the_first_bind_adds_nothing():
    fx = fuseki()
    text = first_bind(fx)
    before = set(fx.graph)
    fx.post(UPDATE, data=text.encode("utf-8"))
    assert set(fx.graph) == before


def test_an_update_naming_an_absent_old_fluent_adds_nothing():
    fx = fuseki()
    first_bind(fx)
    before = set(fx.graph)
    g = rdflib.Graph()
    f.add_goal(g, BIND, 150.0, T1, T1)
    f.close(g, f"{BIND}-goal-19990101T000000000Z", T1)
    fx.post(UPDATE, data=f.guarded_insert(g, [f"{BIND}-goal-19990101T000000000Z"]).encode())
    assert set(fx.graph) == before


def test_an_update_naming_an_already_closed_old_fluent_adds_nothing():
    fx = fuseki()
    first_bind(fx)
    replace_goal(fx)
    before = set(fx.graph)
    g = rdflib.Graph()
    old = f.fluent_iri(BIND, "goal", T0)
    f.add_goal(g, BIND, 175.0, T2, T2)
    f.close(g, old, T2)
    fx.post(UPDATE, data=f.guarded_insert(g, [old]).encode())
    assert set(fx.graph) == before


def test_an_update_naming_an_already_superseded_old_fluent_adds_nothing():
    fx = fuseki()
    first_bind(fx)
    correct_placement(fx)
    before = set(fx.graph)
    g = rdflib.Graph()
    old = f.fluent_iri(BIND, "place", T0)
    f.add_placement(g, BIND, IF0, T0, T2 + HOUR)
    f.supersede(g, old, T2 + HOUR)
    fx.post(UPDATE, data=f.guarded_insert(g, [old]).encode())
    assert set(fx.graph) == before


def test_one_guard_blocks_the_whole_update_if_any_old_fluent_is_stale():
    fx = fuseki()
    first_bind(fx)
    before = set(fx.graph)
    g = rdflib.Graph()
    live = f.fluent_iri(BIND, "goal", T0)
    f.close(g, live, T1)
    f.close(g, f"{BIND}-place-19990101T000000000Z", T1)
    f.add_goal(g, BIND, 150.0, T1, T1)
    fx.post(UPDATE, data=f.guarded_insert(g, [live, f"{BIND}-place-19990101T000000000Z"]).encode())
    assert set(fx.graph) == before


def test_every_update_text_is_one_operation():
    fx = fuseki()
    texts = [first_bind(fx), replace_goal(fx)[0], correct_placement(fx)[0]]
    for text in texts:
        assert len(prepareUpdate(text).algebra) == 1


def test_the_update_is_a_single_insert_with_no_delete():
    fx = fuseki()
    first_bind(fx)
    text, _old = replace_goal(fx)
    assert "INSERT" in text
    assert not re.search(r"\b(DELETE|CLEAR|DROP|LOAD|CREATE)\b", text, re.I)


def test_writes_only_add_triples():
    fx = fuseki()
    first_bind(fx)
    before = set(fx.graph)
    replace_goal(fx)
    correct_placement(fx)
    assert before <= set(fx.graph)


# ---- TC4: what did the broker know at K about V ------------------------------------------


def test_tc4_a_corrected_placement_answers_differently_by_knowledge_time():
    fx = fuseki()
    first_bind(fx)
    replace_goal(fx)
    correct_placement(fx)

    v = T1 + HOUR
    assert read(fx, valid_at=v, known_at=T2 - HOUR).sources == {LAT: ("olt1", "pon0")}
    assert read(fx, valid_at=v, known_at=T2).sources == {LAT: ("olt1", "pon1")}
    assert read(fx, valid_at=v).sources == {LAT: ("olt1", "pon1")}
    # The superseded record is still in the graph.
    old = rdflib.URIRef(f.fluent_iri(BIND, "place", T0))
    assert (old, rdflib.URIRef(NS + "onInterface"), rdflib.URIRef(IF0)) in fx.graph
    assert fx.graph.value(old, rdflib.URIRef(NS + "supersededAt")) is not None


def test_tc4_the_corrected_placement_keeps_its_valid_interval():
    fx = fuseki()
    first_bind(fx)
    correct_placement(fx)
    # Same validStart as the record it corrects: it covers T0 as soon as it is known.
    assert read(fx, valid_at=T0, known_at=T2).sources == {LAT: ("olt1", "pon1")}
    assert read(fx, valid_at=T0, known_at=T2 - HOUR).sources == {LAT: ("olt1", "pon0")}


# ---- fetch_fluents -------------------------------------------------------------------------


def test_fetch_fluents_lists_every_fluent_under_both_kinds():
    fx = fuseki()
    first_bind(fx)
    replace_goal(fx)
    with patch(POST, side_effect=fx.post):
        fluents, interfaces = fetch_fluents("i1", QUERY)
    assert set(fluents) == {(LAT, "goal"), (LAT, "placement")}
    assert sorted(x.value for x in fluents[(LAT, "goal")]) == [100.0, 150.0]
    assert [x.value for x in fluents[(LAT, "placement")]] == [IF0]
    assert interfaces == {IF0: ("olt1", "pon0")}


def test_a_binding_node_with_no_fluent_is_present_with_empty_lists():
    fx = fuseki()
    g = rdflib.Graph()
    f.add_binding(g, BIND, "i1", LAT)
    fx.post(UPDATE, data=f.guarded_insert(g, []).encode())
    with patch(POST, side_effect=fx.post):
        fluents, interfaces = fetch_fluents("i1", QUERY)
    assert fluents == {(LAT, "goal"): [], (LAT, "placement"): []}
    assert interfaces == {}


def test_two_metrics_resolve_independently():
    fx = fuseki()
    first_bind(fx)
    first_bind(fx, goal=10.0, place=IF1, metric=JIT, bind=f"{NS}binding-i1-JitterMetric")
    b = read(fx)
    assert b.bounds == {LAT: 100.0, JIT: 10.0}
    assert b.sources == {LAT: ("olt1", "pon0"), JIT: ("olt1", "pon1")}


def test_two_open_goals_for_one_metric_are_a_duplicate():
    fx = fuseki()
    first_bind(fx)
    g = rdflib.Graph()
    f.add_goal(g, BIND, 150.0, T1, T1)  # opened without closing the old one
    fx.post(UPDATE, data=f.guarded_insert(g, []).encode())
    with pytest.raises(ValueError, match="duplicate binding for metric"):
        read(fx, valid_at=T1 + HOUR)


# ---- guarded_insert: which closings block (bead contextbroker-5xr) ------------------------------


def test_a_correction_of_a_closed_fluent_is_not_blocked_by_its_end():
    fx = fuseki()
    first_bind(fx)
    replace_goal(fx)  # the old goal now has a validEnd
    old = f.fluent_iri(BIND, "goal", T0)
    g = rdflib.Graph()
    f.add_goal(g, BIND, 90.0, T0, T2, end=T1)  # the corrected copy keeps the old interval
    f.supersede(g, old, T2)
    fx.post(UPDATE, data=f.guarded_insert(g, [old], closed_by=("supersededAt",)).encode())
    assert fx.graph.value(rdflib.URIRef(old), rdflib.URIRef(NS + "supersededAt")) is not None
    # the default guard (a replace) refuses the same update
    fx2 = fuseki()
    first_bind(fx2)
    replace_goal(fx2)
    fx2.post(UPDATE, data=f.guarded_insert(g, [old]).encode())
    assert fx2.graph.value(rdflib.URIRef(old), rdflib.URIRef(NS + "supersededAt")) is None
    # a read at K before the correction still gets the old value, after it the corrected one
    assert read(fx, valid_at=T0 + HOUR, known_at=T2 - HOUR).bounds == {LAT: 100.0}
    assert read(fx, valid_at=T0 + HOUR, known_at=T2).bounds == {LAT: 90.0}
    assert read(fx, valid_at=T1 + HOUR).bounds == {LAT: 150.0}


def test_a_superseded_fluent_blocks_even_the_supersede_only_guard():
    fx = fuseki()
    first_bind(fx)
    correct_placement(fx)
    before = set(fx.graph)
    old = f.fluent_iri(BIND, "place", T0)
    g = rdflib.Graph()
    f.add_placement(g, BIND, IF0, T0, T2 + HOUR)
    f.supersede(g, old, T2 + HOUR)
    fx.post(UPDATE, data=f.guarded_insert(g, [old], closed_by=("supersededAt",)).encode())
    assert set(fx.graph) == before


@pytest.mark.parametrize("bad", [(), ("validEnd", "x"), ("pinnedBound",)])
def test_guarded_insert_refuses_an_unknown_guard_predicate(bad):
    g = rdflib.Graph()
    f.add_goal(g, BIND, 1.0, T0, T0)
    with pytest.raises(ValueError):
        f.guarded_insert(g, [f"{BIND}-goal-x"], closed_by=bad)


# ---- bind() end to end: the real write path, read back with the real resolver --------------------

@pytest.fixture(autouse=True)
def _fuseki_credential(monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "testpassword0")


SHAPES = Path(__file__).resolve().parent.parent / "ontologies" / "shacl" / "topology.shacl.ttl"
DATA = "http://fuseki.test/ds/data"
QUOTE = ('@prefix quan: <http://tio.models.tmforum.org/tio/v3.6.0/QuantityOntology/> .\n'
         '@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n'
         '@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n'
         '_:c quan:smaller ( <%s> [ rdf:value "%s"^^xsd:decimal ] ) .\n')


def run_bind(fx, tmp_path, goal, place="olt1/pon0", at=T0, **kw):
    client = Mock()
    client.get_intent.return_value = {"expression": {"expressionValue": QUOTE % (LAT, goal)}}
    with patch("requests.post", side_effect=fx.post):
        return bind("i1", [f"LatencyMetric={place}"], abox_dir=tmp_path / "abox",
                    query_endpoint=QUERY, data_endpoint=DATA, client=client, now=at, **kw)


def assert_conforms(graph, tmp_path):
    path = tmp_path / "whole.ttl"
    path.write_text(graph.serialize(format="turtle"), encoding="utf-8")
    ok, report = validate_shapes(path, SHAPES)
    assert ok is True, report


def val(fx, iri, name):
    return fx.graph.value(rdflib.URIRef(iri), rdflib.URIRef(NS + name))


def test_bind_replace_end_to_end(tmp_path):
    fx = fuseki()
    run_bind(fx, tmp_path, 25, at=T0)
    goal0 = f.fluent_iri(BIND, "goal", T0)
    assert f.parse_dt(str(val(fx, goal0, "validStart"))) == f.parse_dt(str(val(fx, goal0, "recordedAt"))) == T0
    assert val(fx, goal0, "validEnd") is None

    out, applied = run_bind(fx, tmp_path, 30, at=T1, replace=True)
    assert applied and out.exists()
    goal1 = f.fluent_iri(BIND, "goal", T1)
    assert float(val(fx, goal0, "pinnedBound")) == 25.0
    assert f.parse_dt(str(val(fx, goal0, "validEnd"))) == T1
    assert float(val(fx, goal1, "pinnedBound")) == 30.0
    assert f.parse_dt(str(val(fx, goal1, "validStart"))) == T1
    assert val(fx, f.fluent_iri(BIND, "place", T1), "onInterface") is None  # placement unchanged
    assert read(fx, valid_at=T0 + HOUR).bounds == {LAT: 25.0}
    assert read(fx).bounds == {LAT: 30.0}
    assert len(fx.updates()) == 2
    assert_conforms(fx.graph, tmp_path)


def test_bind_correct_tc4(tmp_path):
    fx = fuseki()
    run_bind(fx, tmp_path, 25, at=T0)
    run_bind(fx, tmp_path, 30, at=T1, replace=True)
    goals_before = {t for t in fx.graph if str(t[0]).startswith(BIND + "-goal-")}
    run_bind(fx, tmp_path, 30, place="olt1/pon1", at=T2, correct=True)

    old = f.fluent_iri(BIND, "place", T0)
    new = f.fluent_iri(BIND, "place", T2)
    assert f.parse_dt(str(val(fx, old, "supersededAt"))) == T2
    assert str(val(fx, old, "onInterface")) == IF0  # kept
    assert f.parse_dt(str(val(fx, new, "validStart"))) == T0  # the old start
    assert val(fx, new, "validEnd") is None
    assert f.parse_dt(str(val(fx, new, "recordedAt"))) == T2
    assert {t for t in fx.graph if str(t[0]).startswith(BIND + "-goal-")} == goals_before

    v = T1 + HOUR
    assert read(fx, valid_at=v, known_at=T2 - HOUR).sources == {LAT: ("olt1", "pon0")}
    assert read(fx, valid_at=v, known_at=T2).sources == {LAT: ("olt1", "pon1")}
    assert_conforms(fx.graph, tmp_path)


def test_bind_correct_of_a_goal_keeps_the_valid_interval(tmp_path):
    fx = fuseki()
    run_bind(fx, tmp_path, 25, at=T0)
    run_bind(fx, tmp_path, 30, at=T1, replace=True)
    run_bind(fx, tmp_path, 28, at=T2, correct=True)
    new = f.fluent_iri(BIND, "goal", T2)
    assert f.parse_dt(str(val(fx, new, "validStart"))) == T1  # the corrected (open) goal's start
    assert val(fx, new, "validEnd") is None
    assert read(fx, valid_at=T1 + HOUR, known_at=T2 - HOUR).bounds == {LAT: 30.0}
    assert read(fx, valid_at=T1 + HOUR).bounds == {LAT: 28.0}
    assert read(fx, valid_at=T0 + HOUR).bounds == {LAT: 25.0}  # the closed goal is untouched
    assert_conforms(fx.graph, tmp_path)


def test_bind_replace_valid_from_reissue(tmp_path):
    fx = fuseki()
    run_bind(fx, tmp_path, 25, at=T0)
    v0 = T0 + DAY
    run_bind(fx, tmp_path, 30, at=T1, replace=True, valid_from="2026-11-02T10:00:00Z")

    old = f.fluent_iri(BIND, "goal", T0)
    copy = f.fluent_iri(BIND, "goal-reissue", T1)
    new = f.fluent_iri(BIND, "goal", T1)
    assert f.parse_dt(str(val(fx, old, "supersededAt"))) == T1
    assert float(val(fx, copy, "pinnedBound")) == 25.0
    assert f.parse_dt(str(val(fx, copy, "validStart"))) == T0
    assert f.parse_dt(str(val(fx, copy, "validEnd"))) == v0
    assert f.parse_dt(str(val(fx, copy, "recordedAt"))) == T1
    assert f.parse_dt(str(val(fx, new, "validStart"))) == v0
    assert float(val(fx, new, "pinnedBound")) == 30.0

    between = v0 + HOUR  # after the goal really changed, before the run
    assert read(fx, valid_at=between, known_at=T1 - HOUR).bounds == {LAT: 25.0}
    assert read(fx, valid_at=between).bounds == {LAT: 30.0}
    assert read(fx, valid_at=v0 - HOUR).bounds == {LAT: 25.0}
    assert_conforms(fx.graph, tmp_path)


def test_bind_replace_without_valid_from_is_inconclusive_for_the_gap_not_wrong(tmp_path):
    # Research Pitfall 10: asked with a knowledge time before the run about a valid time after
    # it, the closed goal does not cover and the new one is not yet known: a named gap.
    fx = fuseki()
    run_bind(fx, tmp_path, 25, at=T0)
    run_bind(fx, tmp_path, 30, at=T1, replace=True)
    assert read(fx, valid_at=T1 + HOUR, known_at=T1 - HOUR).gaps == ((LAT, "goal"),)


def test_bind_replay_changes_nothing(tmp_path):
    fx = fuseki()
    run_bind(fx, tmp_path, 25, at=T0)
    before = set(fx.graph)
    assert run_bind(fx, tmp_path, 25, at=T1, replace=True) == (None, False)
    assert set(fx.graph) == before
