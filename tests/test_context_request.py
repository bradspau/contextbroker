"""POST /context/request end to end: a minted token, the real route, the real SPARQL readers over the
rdflib stand-in tests.fluent_helpers.GraphFuseki, and a fake VictoriaMetrics.

The tracked demo topology (ontologies/abox/topology-demo.ttl) is the topology fixture. Every refusal
test asserts that neither Fuseki nor VictoriaMetrics nor the TMF921 client was touched. Times are fixed:
NOW is the one clock read of the handler.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import rdflib
import requests
from fastapi.testclient import TestClient

from broker_engine import main
from broker_state.process import get_store
from broker_state.store import NewEvent
from data_contracts import fluents as f
from data_contracts.context_contract import (
    CONTEXT_CLASSES, PURPOSES, RELATIONSHIP_TYPES, ClassEligibility, Eligibility, situation_id)
from intent_management import tmf921_source
from tests.auth_helpers import make_token
from tests.fluent_helpers import (
    IF0, IF1, IFACES_TTL, LAT, NS, T0, T1, UPDATE_URL, GraphFuseki, bind, replace_goal)

DEMO = Path(__file__).resolve().parent.parent / "ontologies" / "abox" / "topology-demo.ttl"
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
BOTH = ["has_interface", "connected_to"]
TOPOLOGY = {"purpose": "diagnosis", "scope": {"device": "Router-A"}, "classes": ["topology"],
            "max_depth": 3, "relationship_types": BOTH}
WIDE = {"entities": ["*"], "purposes": ["diagnosis"], "classes": ["topology"]}
A_IF = "Router-A/GigabitEthernet0/0/0"
B_IF = "Router-B/GigabitEthernet0/0/1"

client = TestClient(main.app)


class FakeVictoriaMetrics:
    """Answers the joiner's two instant queries from `series`: (series name, device, interface) -> a
    list of (value, sample epoch seconds), one entry per matching series."""

    def __init__(self):
        self.series = {}
        self.queries = []
        self.params = []

    def add(self, metric_type, device, interface, *samples):
        self.series[(f"interface_metrics_{metric_type}", device, interface)] = list(samples)

    def get(self, url, params=None, timeout=None):
        query = params["query"]
        self.queries.append(query)
        self.params.append(dict(params))
        name, device, interface = re.search(
            r'(\w+)\{device="([^"]+)", interface="([^"]+)"\}', query).groups()
        rows = self.series.get((name, device, interface), [])
        if query.startswith("min(tlast_over_time("):
            result = [{"metric": {}, "value": [0, str(min(at for _, at in rows))]}] if rows else []
        else:
            result = [{"metric": {"series": str(n)}, "value": [0, str(value)]}
                      for n, (value, _) in enumerate(rows)]
        reply = Mock()
        reply.raise_for_status.return_value = None
        reply.json.return_value = {"data": {"result": result}}
        return reply


class Upstreams:
    """What the route reached: Fuseki (the stand-in), VictoriaMetrics, and the TMF921 client."""

    def __init__(self, fx, get, vm, get_intent, push, fulfilment):
        self.fx, self.get, self.vm = fx, get, vm
        self.get_intent, self.push, self.fulfilment = get_intent, push, fulfilment

    def queries(self):
        return [kw["data"]["query"] for url, kw in self.fx.posts if url.endswith("/query")]

    def untouched(self):
        assert self.queries() == []
        self.get.assert_not_called()
        self.get_intent.assert_not_called()
        self.push.assert_not_called()
        self.fulfilment.assert_not_called()


def demo_graph():
    g = rdflib.Graph()
    g.parse(DEMO, format="turtle")
    return g


def ifaces_graph():
    g = rdflib.Graph()
    g.parse(data=IFACES_TTL, format="turtle")
    return g


def _upstreams(monkeypatch, graph):
    """The graph in the Fuseki stand-in; every upstream spied; the clock fixed at NOW. A request must
    never push, mark or record: the teardown of each user checks the spies and the mark table."""
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "testpassword0")
    fx, vm = GraphFuseki(graph), FakeVictoriaMetrics()
    with patch("requests.post", fx.post), patch("requests.get", side_effect=vm.get) as get, \
            patch("broker_engine.context_request._utcnow", return_value=NOW), \
            patch("intent_management.tmf921_client.Tmf921Client.get_intent") as get_intent, \
            patch("intent_management.tmf921_client.Tmf921Client.post_observation") as push, \
            patch("intent_management.tmf921_source.TMF921_STORE.fulfilment_update") as fulfilment:
        seen = Upstreams(fx, get, vm, get_intent, push, fulfilment)
        yield seen
        push.assert_not_called()
        fulfilment.assert_not_called()
        assert tmf921_source.TMF921_STORE.mark_count() == 0


@pytest.fixture
def rig(monkeypatch):
    """The demo topology."""
    yield from _upstreams(monkeypatch, demo_graph())


@pytest.fixture
def olt(monkeypatch):
    """Two interfaces of olt1 and no binding yet."""
    yield from _upstreams(monkeypatch, ifaces_graph())


def token(authority=WIDE, scopes=("context:read",), **claims):
    return {"Authorization": "Bearer " + make_token(
        scopes, **({"authority": authority} if authority is not None else {}), **claims)}


def post(body, headers=None, **kw):
    return client.post("/context/request", json=body, headers=token() if headers is None else headers, **kw)


# ---- the tracer: topology from a token to a Context Graph ----

def test_topology_request_returns_situation_graph_eligibility_and_expiry(rig):
    r = post(TOPOLOGY)
    assert r.status_code == 200, r.text
    out = r.json()
    expected_id = situation_id(
        requester="test-caller", purpose="diagnosis", device="Router-A", interface=None, intent_id=None,
        valid_at=NOW, known_at=None, classes=["topology"], max_depth=3, relationship_types=BOTH)
    assert out["situation"] == {
        "id": expected_id, "requester": "test-caller", "purpose": "diagnosis",
        "scope": {"device": "Router-A", "interface": None}, "intent_id": None, "classes": ["topology"],
        "max_depth": 3, "relationship_types": sorted(BOTH), "valid_at": NOW.isoformat(),
        "known_at": None, "retrieved_at": NOW.isoformat(), "historical": False, "window_seconds": None}
    assert out["eligibility"] == {
        "result": "eligible", "classes": {"topology": {"result": "eligible", "reasons": []}},
        "reasons": []}
    assert [n["id"] for n in out["graph"]["nodes"]] == ["Router-A", A_IF, B_IF, "Router-B"]
    assert len(out["graph"]["edges"]) == 3
    assert out["graph"]["limits"]["cut_by"] == []
    assert {"from", "to", "type"} <= set(out["graph"]["edges"][0])
    [link] = out["fluent_values"]
    assert link["class"] == "topology" and link["kind"] == "link"
    assert link["provenance"]["source_system"] == "fuseki"
    assert out["envelopes"] == []
    assert out["expires_at"] == (NOW + timedelta(seconds=300)).isoformat()
    assert out["expiry_basis"] == "configured_state"
    assert out["verdict"] is None
    # A read: only Fuseki was reached, with no update.
    rig.get.assert_not_called()
    rig.get_intent.assert_not_called()
    rig.push.assert_not_called()
    assert rig.fx.updates() == []


def test_the_requester_is_the_token_subject(rig):
    r = post(TOPOLOGY, headers=token(sub="alice-7"))
    assert r.json()["situation"]["requester"] == "alice-7"
    assert r.json()["situation"]["id"] != situation_id(
        requester="test-caller", purpose="diagnosis", device="Router-A", interface=None, intent_id=None,
        valid_at=NOW, known_at=None, classes=["topology"], max_depth=3, relationship_types=BOTH)


def test_the_same_question_gives_the_same_id_and_a_different_shape_another(rig):
    first = post(TOPOLOGY).json()["situation"]["id"]
    assert post(TOPOLOGY).json()["situation"]["id"] == first
    assert post({**TOPOLOGY, "max_depth": 2}).json()["situation"]["id"] != first


def test_authority_over_router_a_only_cuts_the_graph_and_stays_eligible(rig):
    r = post(TOPOLOGY, headers=token({**WIDE, "entities": ["Router-A"]}))
    out = r.json()
    assert r.status_code == 200
    assert [n["id"] for n in out["graph"]["nodes"]] == ["Router-A", A_IF]
    assert out["graph"]["limits"]["cut_by"] == ["authority"]
    assert out["eligibility"]["result"] == "eligible"
    assert B_IF not in r.text and "Router-B" not in r.text


# ---- the response model is wire neutral and complete ----

def test_the_openapi_document_lists_the_route_and_the_response_model_without_claim_values(rig):
    spec = client.get("/openapi.json").json()
    operation = spec["paths"]["/context/request"]["post"]
    assert "execution authority" in operation["description"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ContextResponse")
    text = str(spec)
    assert "test-caller" not in text and "Router-A" not in text
    rig.untouched()


# ---- refusals: before any upstream call ----

REFUSED_403 = {
    "no_authority_claim": (token(None), {"detail": "authority claims required"}),
    "entity": (token({**WIDE, "entities": ["Router-B"]}), {"detail": "outside authority scope: entity"}),
    "purpose": (token({**WIDE, "purposes": ["reporting"]}), {"detail": "outside authority scope: purpose"}),
    "class": (token({**WIDE, "classes": ["goals"]}), {"detail": "outside authority scope: class"}),
    "assess_scope_only": (token(WIDE, scopes=("context:assess",)), {"detail": "insufficient scope"}),
}


@pytest.mark.parametrize("case", sorted(REFUSED_403))
def test_unauthorised_requests_are_403_with_a_fixed_reason_and_no_upstream_call(case, rig):
    headers, detail = REFUSED_403[case]
    r = post(TOPOLOGY, headers=headers)
    assert r.status_code == 403
    assert r.json() == detail
    rig.untouched()


def _without(key):
    return {k: v for k, v in TOPOLOGY.items() if k != key}


BAD_SHAPES = {
    "unknown_field": {**TOPOLOGY, "colour": "red"},
    "requester_field": {**TOPOLOGY, "requester": "mallory"},
    "missing_purpose": _without("purpose"),
    "missing_scope": _without("scope"),
    "missing_classes": _without("classes"),
    "unknown_purpose": {**TOPOLOGY, "purpose": "mischief"},
    "max_depth_four": {**TOPOLOGY, "max_depth": 4},
    "max_depth_text": {**TOPOLOGY, "max_depth": "2"},
    "repeated_class": {**TOPOLOGY, "classes": ["topology", "topology"]},
    "topology_without_depth": _without("max_depth"),
    "topology_without_types": _without("relationship_types"),
    "goals_without_intent": {**TOPOLOGY, "classes": ["topology", "goals"]},
    "unsafe_device": {**TOPOLOGY, "scope": {"device": 'R"; DROP'}},
}


@pytest.mark.parametrize("case", sorted(BAD_SHAPES))
def test_malformed_requests_are_422_and_reach_no_upstream(case, rig):
    r = post(BAD_SHAPES[case])
    assert r.status_code == 422
    rig.untouched()


def test_a_query_parameter_is_422_and_reaches_no_upstream(rig):
    r = post(TOPOLOGY, params={"debug": "1"})
    assert r.status_code == 422
    rig.untouched()


@pytest.mark.parametrize("extra,detail", [
    ({"as_of": "31d"}, "as_of is further back than the retention period"),
    ({"known_at": "31d"}, "known_at is further back than the retention period"),
    ({"freshness": "6m"}, "freshness must be from 1 s to 300 s"),
])
def test_offsets_and_freshness_beyond_their_limits_are_400_before_any_upstream_call(extra, detail, rig):
    r = post({**TOPOLOGY, **extra})
    assert r.status_code == 400
    assert r.json() == {"detail": detail}
    rig.untouched()


def test_a_refused_request_logs_neither_the_requester_nor_the_scope(rig, caplog):
    with caplog.at_level(logging.DEBUG):
        post(TOPOLOGY, headers=token({**WIDE, "entities": ["Router-B"]}, sub="canary-requester-31"))
    assert "canary-requester-31" not in caplog.text
    assert "Router-A" not in caplog.text


# ---- time ----

def test_a_past_request_expires_at_retrieval_and_reads_the_topology_then(rig):
    r = post({**TOPOLOGY, "as_of": "1h"})
    out = r.json()
    assert r.status_code == 200
    assert out["situation"]["historical"] is True
    assert out["situation"]["valid_at"] == (NOW - timedelta(hours=1)).isoformat()
    assert out["expires_at"] == NOW.isoformat()
    assert out["expiry_basis"] == "historical_request"
    assert len(out["graph"]["nodes"]) == 4


def test_a_past_request_before_the_link_existed_has_the_interface_but_no_link(rig):
    r = post({**TOPOLOGY, "scope": {"device": "Router-A", "interface": "GigabitEthernet0/0/0"},
              "as_of": "2d"})
    out = r.json()
    assert out["graph"]["edges"] == [
        {"from": "Router-A", "to": A_IF, "type": "has_interface", "link": None, "fluent": None}]
    assert out["fluent_values"] == []
    assert out["eligibility"]["result"] == "eligible"


def test_known_at_alone_makes_the_request_historical(rig):
    out = post({**TOPOLOGY, "known_at": "1h"}).json()
    assert out["situation"]["known_at"] == (NOW - timedelta(hours=1)).isoformat()
    assert out["situation"]["historical"] is True
    assert out["expiry_basis"] == "historical_request"


def test_a_tighter_freshness_shortens_the_configured_expiry(rig):
    out = post({**TOPOLOGY, "freshness": "1m"}).json()
    assert out["expires_at"] == (NOW + timedelta(seconds=60)).isoformat()


# ---- identity and findings ----

def test_an_unknown_scope_entity_is_a_gap_with_no_evidence(rig):
    out = post({**TOPOLOGY, "scope": {"device": "Router-Z"}}).json()
    assert out["eligibility"]["classes"]["topology"] == {
        "result": "gap", "reasons": ["scope entity not in the topology"]}
    assert out["graph"]["nodes"] == []
    assert out["expires_at"] == NOW.isoformat()
    assert out["expiry_basis"] == "no_evidence"


def test_a_twin_interface_node_makes_the_scope_ambiguous(rig):
    rig.fx.graph.parse(data="""
        @prefix : <http://example.org/network-ontology#> .
        :if-router-a-ge000-twin a :Interface ; :onDevice "Router-A" ;
            :interfaceName "GigabitEthernet0/0/0" .""", format="turtle")
    out = post({**TOPOLOGY, "scope": {"device": "Router-A", "interface": "GigabitEthernet0/0/0"}}).json()
    assert out["eligibility"]["result"] == "ineligible"
    assert out["eligibility"]["classes"]["topology"] == {
        "result": "ineligible", "reasons": ["ambiguous identity: scope entity"]}


def test_a_fuseki_outage_is_the_existing_502(rig):
    with patch("requests.post", side_effect=requests.ConnectionError("tcp://secret-host:3030 refused")):
        r = post(TOPOLOGY)
    assert r.status_code == 502
    assert r.json() == {"detail": "upstream data store unavailable"}
    assert "secret-host" not in r.text


# ---- goals and observations: one fluent snapshot, one telemetry read each ----

ASSURANCE = {"entities": ["*"], "purposes": ["assurance"], "classes": ["topology", "goals", "observations"]}
SCOPES = ("context:read", "context:assess")
OLT_PON0 = {"device": "olt1", "interface": "pon0"}
ASK = {"purpose": "assurance", "scope": OLT_PON0, "intent_id": "i1", "classes": ["goals", "observations"]}
SHORT = "LatencyMetric"
IF2 = NS + "if-olt2-pon0"


def assure(body=None, authority=ASSURANCE, scopes=SCOPES):
    return post(ASK if body is None else body, headers=token(authority, scopes=scopes))


def sample(rig, value=50.0, age_s=30):
    rig.vm.add("latency_ms", "olt1", "pon0", (value, (NOW - timedelta(seconds=age_s)).timestamp()))


def reasons(out, name):
    return out["eligibility"]["classes"][name]


def test_goals_and_observations_join_with_provenance_and_one_read_of_each_source(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    live_goal(olt)
    seen_at = (NOW - timedelta(seconds=30)).isoformat()
    r = assure()
    out = r.json()
    assert r.status_code == 200, r.text
    assert out["eligibility"]["result"] == "eligible"
    assert reasons(out, "goals") == {"result": "eligible", "reasons": []}
    assert reasons(out, "observations") == {"result": "eligible", "reasons": []}
    [envelope] = out["envelopes"]
    assert envelope["metric"] == SHORT and envelope["node"] == "olt1/pon0"
    assert envelope["entity_id"] == "olt1:pon0" and envelope["value"] == 50.0
    assert envelope["observed_at"] == seen_at
    assert envelope["provenance"] == [{"source_system": "victoriametrics",
                                       "reference": "interface_metrics_latency_ms",
                                       "observed_at": seen_at, "retrieved_at": NOW.isoformat()}]
    by_kind = {v["kind"]: v for v in out["fluent_values"]}
    assert set(by_kind) == {"goal", "placement"}
    assert (by_kind["goal"]["subject"], by_kind["goal"]["value"], by_kind["goal"]["in_force"]) == (
        SHORT, 100.0, True)
    assert (by_kind["placement"]["subject"], by_kind["placement"]["value"],
            by_kind["placement"]["in_force"]) == (SHORT, "olt1/pon0", True)
    for value in by_kind.values():
        assert value["class"] in ("goals",)
        assert value["valid_from"] == T0.isoformat() and value["valid_to"] is None
        assert value["provenance"] == {
            "source_system": "fuseki", "reference": value["provenance"]["reference"],
            "observed_at": T0.isoformat(), "retrieved_at": NOW.isoformat()}
        assert value["provenance"]["reference"].startswith(NS)
    assert out["expires_at"] == (NOW - timedelta(seconds=30) + timedelta(seconds=300)).isoformat()
    assert out["expiry_basis"] == "oldest_observation"
    assert out["graph"] is None and out["verdict"]["verdict"] == "success"
    assert len([q for q in olt.queries() if "ServiceBinding" in q]) == 1
    assert olt.vm.queries and all('device="olt1"' in q for q in olt.vm.queries)
    assert len(olt.vm.queries) == 2  # the value and its sample time: one read of the metric
    assert olt.get_intent.call_count == 1  # the verdict's one GET


def test_a_read_token_naming_an_intent_is_refused_before_any_upstream_call(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    before = len(olt.queries())
    r = assure(scopes=("context:read",))
    assert r.status_code == 403 and r.json() == {"detail": "insufficient scope"}
    assert len(olt.queries()) == before
    olt.get.assert_not_called()


def test_a_placement_outside_the_callers_entities_is_never_read(olt):
    olt.fx.graph.parse(data=f"""@prefix : <{NS}> .
        :if-olt2-pon0 a :Interface ; :onDevice "olt2" ; :interfaceName "pon0" .""", format="turtle")
    bind(olt.fx, "i1", LAT, 100.0, IF2, T0)
    link = rdflib.Graph()
    f.add_link_fluent(link, NS + "link-olt1-olt2", IF0, IF2, T0, T0)
    olt.fx.graph += link
    sample(olt)
    olt.vm.add("latency_ms", "olt2", "pon0", (7.0, (NOW - timedelta(seconds=5)).timestamp()))
    narrow = {**ASSURANCE, "entities": ["olt1"]}
    body = {**ASK, "classes": ["topology", "goals", "observations"], "max_depth": 3,
            "relationship_types": ["has_interface", "connected_to"]}
    r = assure(body, authority=narrow)
    out = r.json()
    assert r.status_code == 200, r.text
    assert reasons(out, "goals") == {"result": "ineligible",
                                     "reasons": ["placement outside authority scope: LatencyMetric"]}
    assert reasons(out, "observations") == {"result": "gap", "reasons": ["no observation: LatencyMetric"]}
    assert olt.vm.queries == []
    assert out["envelopes"] == []
    assert [v["kind"] for v in out["fluent_values"] if v["class"] == "goals"] == ["goal"]
    assert all("olt2" not in str(v["value"]) for v in out["fluent_values"])
    assert not any(n["device"] == "olt2" for n in out["graph"]["nodes"])
    assert "cut_by" in out["graph"]["limits"] and "authority" in out["graph"]["limits"]["cut_by"]
    assert "olt2" not in r.text


def test_a_tighter_freshness_makes_an_older_sample_stale_and_shortens_the_expiry(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt, age_s=120)
    out = assure({**ASK, "freshness": "1m"}).json()
    assert reasons(out, "observations") == {"result": "ineligible",
                                            "reasons": ["stale observation: LatencyMetric"]}
    assert out["eligibility"]["result"] == "ineligible"
    assert out["expires_at"] == (NOW - timedelta(seconds=120) + timedelta(seconds=60)).isoformat()


def test_observations_report_a_sample_older_than_the_default_limit_as_stale_not_missing(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt, age_s=400)
    out = assure().json()
    assert reasons(out, "observations")["reasons"] == ["stale observation: LatencyMetric"]


def test_an_intent_with_no_binding_is_a_gap_in_goals_and_observations_and_reads_no_telemetry(olt):
    out = assure({**ASK, "intent_id": "i9"}).json()
    assert reasons(out, "goals") == {"result": "gap", "reasons": ["no binding for intent"]}
    assert reasons(out, "observations") == {"result": "gap", "reasons": ["no binding for intent"]}
    assert out["eligibility"]["result"] == "gap"
    assert out["fluent_values"] == [] and out["envelopes"] == []
    assert out["expires_at"] == NOW.isoformat() and out["expiry_basis"] == "no_evidence"
    olt.get.assert_not_called()


def test_a_goal_replaced_inside_the_window_makes_goals_ineligible_and_both_goals_are_listed(olt):
    binding = bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    replace_goal(olt.fx, binding, 80.0, NOW - timedelta(seconds=100), T0)
    sample(olt)
    out = assure().json()
    assert reasons(out, "goals") == {"result": "ineligible",
                                     "reasons": ["goal changed within window: LatencyMetric"]}
    goals = [v for v in out["fluent_values"] if v["kind"] == "goal"]
    assert [(v["value"], v["in_force"]) for v in sorted(goals, key=lambda v: v["valid_from"])] == [
        (100.0, False), (80.0, True)]
    assert out["eligibility"]["result"] == "ineligible"


def test_a_goal_replaced_long_ago_is_eligible_and_only_the_current_goal_is_listed(olt):
    binding = bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    replace_goal(olt.fx, binding, 80.0, NOW - timedelta(hours=2), T0)
    sample(olt)
    out = assure().json()
    assert reasons(out, "goals")["result"] == "eligible"
    assert [v["value"] for v in out["fluent_values"] if v["kind"] == "goal"] == [80.0]


def test_a_non_finite_sample_makes_observations_a_gap_never_eligible(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt, value=float("nan"))
    out = assure().json()
    assert reasons(out, "observations") == {"result": "gap", "reasons": ["no observation: LatencyMetric"]}
    assert out["eligibility"]["result"] == "gap"
    assert out["envelopes"][0]["value"] is None and out["envelopes"][0]["quality"]["missing"] is True


def test_two_matching_series_make_observations_ineligible_not_eligible(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    at = (NOW - timedelta(seconds=30)).timestamp()
    olt.vm.add("latency_ms", "olt1", "pon0", (50.0, at), (60.0, at))
    out = assure().json()
    assert reasons(out, "observations") == {"result": "ineligible",
                                            "reasons": ["conflicting observation: LatencyMetric"]}


def test_two_placements_in_force_fail_closed_and_read_no_telemetry(olt):
    binding = bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    extra = rdflib.Graph()
    f.add_placement(extra, binding, IF1, T1, T1)
    olt.fx.graph += extra
    sample(olt)
    out = assure().json()
    assert reasons(out, "goals") == {"result": "ineligible",
                                     "reasons": ["duplicate placement in force: LatencyMetric"]}
    assert reasons(out, "observations") == {"result": "ineligible", "reasons": ["invalid binding"]}
    assert olt.vm.queries == []
    assert out["envelopes"] == []


def test_topology_goals_and_observations_share_one_node_id(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    body = {**ASK, "classes": ["topology", "goals", "observations"], "max_depth": 1,
            "relationship_types": ["has_interface", "connected_to"]}
    out = assure(body).json()
    node_ids = {n["id"] for n in out["graph"]["nodes"]}
    assert "olt1/pon0" in node_ids
    assert {e["node"] for e in out["envelopes"]} <= node_ids
    assert out["envelopes"][0]["node"] == "olt1/pon0"
    assert out["eligibility"]["result"] == "eligible"


def test_a_goals_only_request_reads_no_telemetry_and_expires_on_the_configured_basis(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    out = assure({**ASK, "classes": ["goals"]}).json()
    assert list(out["eligibility"]["classes"]) == ["goals"]
    assert out["envelopes"] == []
    olt.get.assert_not_called()
    assert out["expiry_basis"] == "configured_state"
    assert out["expires_at"] == (NOW + timedelta(seconds=300)).isoformat()


def test_a_past_request_reads_the_telemetry_then_and_expires_at_retrieval(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    then = NOW - timedelta(hours=1)
    olt.vm.add("latency_ms", "olt1", "pon0", (50.0, (then - timedelta(seconds=30)).timestamp()))
    out = assure({**ASK, "as_of": "1h"}).json()
    assert out["situation"]["historical"] is True
    assert out["situation"]["valid_at"] == then.isoformat()
    assert reasons(out, "observations")["result"] == "eligible"
    assert all(float(p["time"]) == then.timestamp() for p in olt.vm.params)
    assert out["expires_at"] == NOW.isoformat() and out["expiry_basis"] == "historical_request"
    olt.get_intent.assert_not_called()


def test_a_request_never_pushes_marks_or_records(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    for body in (ASK, {**ASK, "as_of": "1h"}, {**ASK, "known_at": "1h"}):
        assert assure(body).status_code == 200
    olt.push.assert_not_called()
    olt.fulfilment.assert_not_called()
    assert tmf921_source.TMF921_STORE.mark_count() == 0
    assert len(olt.fx.updates()) == 1  # the binding written by the test, nothing after it


# ---- the verdict: the read-only TMF921 assess, built on the same reads as the eligibility ----

def _ttl(uri, val):
    return ('@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n'
            f'[] <http://x/p> ( <{uri}> [ rdf:value {val} ] ) .')


def tmf921_intent(*pairs):
    """An ACTIVE TMF921 intent document carrying these (metric URI, goal) pairs."""
    return {"lifecycleStatus": "ACTIVE",
            "expression": {"expressionValue": "\n".join(_ttl(u, v) for u, v in pairs)}}


def live_goal(rig, goal=100):
    """The TMF921 server answers a GET of the intent with this goal."""
    rig.get_intent.return_value = tmf921_intent((LAT, goal))


VERDICT_KEYS = {"verdict", "per_interface", "reasons", "dropped_count", "intent_status", "last_outcome",
                "source", "mode", "as_of", "evaluated_at", "requirement"}


def test_verdict_success_is_the_read_only_assess_on_the_same_reads_as_the_eligibility(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    live_goal(olt)
    out = assure().json()
    verdict = out["verdict"]
    assert verdict["verdict"] == "success"
    assert set(verdict) == VERDICT_KEYS  # the duplicating "context" and "goals" keys are dropped
    assert verdict["source"] == "tmf921" and verdict["mode"] == "at" and verdict["dropped_count"] == 0
    assert out["eligibility"]["result"] == "eligible"
    assert olt.get_intent.call_count == 1  # a current request makes at most one TMF921 GET
    assert len(olt.vm.queries) == 2  # the verdict reused the eligibility's read of the one metric
    assert len([q for q in olt.queries() if "ServiceBinding" in q]) == 1  # one fluent snapshot
    olt.push.assert_not_called()
    olt.fulfilment.assert_not_called()
    assert tmf921_source.TMF921_STORE.mark_count() == 0


def test_verdict_of_a_past_request_makes_no_tmf921_call(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    then = NOW - timedelta(minutes=1)
    olt.vm.add("latency_ms", "olt1", "pon0", (50.0, (then - timedelta(seconds=30)).timestamp()))
    out = assure({**ASK, "as_of": "1m"}).json()
    assert out["verdict"]["verdict"] == "success"
    assert out["verdict"]["as_of"] == "1m"
    olt.get_intent.assert_not_called()
    assert len(olt.vm.queries) == 2
    assert len([q for q in olt.queries() if "ServiceBinding" in q]) == 1


def test_verdict_failure_when_the_sample_breaches_the_goal(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt, value=150.0)
    live_goal(olt)
    out = assure().json()
    assert out["verdict"]["verdict"] == "failure"
    assert reasons(out, "observations")["result"] == "eligible"  # the verdict is not the eligibility


def test_verdict_is_inconclusive_for_a_stale_sample(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt, age_s=400)
    live_goal(olt)
    out = assure().json()
    assert out["verdict"]["verdict"] == "inconclusive"
    assert reasons(out, "observations")["reasons"] == ["stale observation: LatencyMetric"]


@pytest.mark.parametrize("value", [50.0, 150.0, float("nan")])
def test_verdict_value_is_always_one_of_the_three(value, olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt, value=value)
    live_goal(olt)
    assert assure().json()["verdict"]["verdict"] in ("success", "failure", "inconclusive")


def test_verdict_is_null_without_the_observations_class(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    live_goal(olt)
    out = assure({**ASK, "classes": ["goals"]}).json()
    assert out["verdict"] is None
    olt.get_intent.assert_not_called()


def test_verdict_stays_when_topology_is_added(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    live_goal(olt)
    body = {**ASK, "classes": ["topology", "goals", "observations"], "max_depth": 1,
            "relationship_types": ["has_interface", "connected_to"]}
    out = assure(body).json()
    assert out["verdict"]["verdict"] == "success"
    assert out["graph"] is not None


def test_verdict_is_null_and_no_tmf921_call_when_a_placement_is_outside_authority(olt):
    olt.fx.graph.parse(data=f"""@prefix : <{NS}> .
        :if-olt2-pon0 a :Interface ; :onDevice "olt2" ; :interfaceName "pon0" .""", format="turtle")
    bind(olt.fx, "i1", LAT, 100.0, IF2, T0)
    olt.vm.add("latency_ms", "olt2", "pon0", (7.0, (NOW - timedelta(seconds=5)).timestamp()))
    live_goal(olt)
    out = assure(authority={**ASSURANCE, "entities": ["olt1"]}).json()
    assert out["verdict"] is None
    olt.get_intent.assert_not_called()
    assert olt.vm.queries == []


def test_verdict_for_an_intent_with_no_binding_is_inconclusive_with_a_fixed_reason(olt):
    out = assure({**ASK, "intent_id": "i9"}).json()
    assert out["verdict"]["verdict"] == "inconclusive"
    assert out["verdict"]["reasons"] == ["no binding for intent"]
    olt.get_intent.assert_not_called()
    olt.get.assert_not_called()
    assert len([q for q in olt.queries() if "ServiceBinding" in q]) == 1


def test_verdict_with_two_placements_in_force_is_inconclusive_invalid_binding(olt):
    binding = bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    extra = rdflib.Graph()
    f.add_placement(extra, binding, IF1, T1, T1)
    olt.fx.graph += extra
    sample(olt)
    live_goal(olt)
    out = assure().json()
    assert out["verdict"]["verdict"] == "inconclusive"
    assert out["verdict"]["reasons"] == ["invalid binding"]
    # The one case in which the binding is read twice: the assess resolves and refuses on its own.
    assert len([q for q in olt.queries() if "ServiceBinding" in q]) == 2
    olt.get_intent.assert_not_called()
    assert olt.vm.queries == []


# ---- eligibility and verdict are independent; a changed goal is caught through the route ----

def test_verdict_and_eligibility_disagree_by_design_when_the_live_goal_differs_from_the_pin(olt):
    # The verdict compares the live TMF921 goal with the pin, while eligibility reads the fluents only
    # (the goal pinned in the graph, whose history is what the window judges). Flow 3: the server
    # replaced the goal with the observed value, so the fluent-based goals class is eligible while the
    # verdict is inconclusive. Neither feeds the other (Ph3 D-16, D-19).
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    live_goal(olt, 150)
    out = assure().json()
    assert reasons(out, "goals") == {"result": "eligible", "reasons": []}
    assert out["verdict"]["verdict"] == "inconclusive"
    assert out["verdict"]["reasons"][0].startswith("goal changed: LatencyMetric pinned 100, now 150")
    assert out["verdict"]["intent_status"] == "unknown"
    olt.push.assert_not_called()


def test_independent_eligibility_does_not_change_the_verdict(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    live_goal(olt)
    alone = assure().json()
    fixed = Eligibility(result="ineligible", reasons=["stale observation: LatencyMetric"],
                        classes={"goals": ClassEligibility(result="ineligible", reasons=[])})
    with patch("broker_engine.context_request.overall", return_value=fixed) as overall:
        patched = assure().json()
    assert patched["eligibility"]["result"] == "ineligible"
    assert patched["verdict"] == alone["verdict"] and patched["verdict"]["verdict"] == "success"
    [(args, kwargs)] = [(c.args, c.kwargs) for c in overall.call_args_list]
    assert all(isinstance(v, ClassEligibility) for v in args[0].values())  # only class labels go in
    assert not kwargs


def test_independent_verdict_does_not_change_the_eligibility(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    live_goal(olt)
    alone = assure().json()
    fixed = {"verdict": "failure", "reasons": ["LatencyMetric: forced"], "context": [], "goals": []}
    with patch("broker_engine.context_request.assess_tmf921_as_of",
               side_effect=lambda *a, **kw: dict(fixed)) as judge:
        patched = assure().json()
    assert patched["verdict"] == {"verdict": "failure", "reasons": ["LatencyMetric: forced"]}
    assert patched["eligibility"] == alone["eligibility"]
    [call] = judge.call_args_list
    for value in (*call.args, *call.kwargs.values()):  # nothing the assess is given is a label
        assert not isinstance(value, (Eligibility, ClassEligibility))
        assert "eligib" not in repr(value).lower()


def _reissue(fx, binding, new_goal, change_at, recorded_at, old_recorded=T0):
    """What `bind_intent --replace --valid-from` writes: the old goal superseded, a copy of it ending at
    `change_at`, and the new goal starting then, all recorded at `recorded_at`."""
    old = f.fluent_iri(binding, "goal", old_recorded)
    g = rdflib.Graph()
    f.supersede(g, old, recorded_at)
    f.add_goal(g, binding, 100.0, old_recorded, recorded_at, change_at, kind="goal-reissue")
    f.add_goal(g, binding, new_goal, change_at, recorded_at)
    fx.post(UPDATE_URL, data=f.guarded_insert(g, [old]).encode("utf-8"))


def _correct_goal(fx, binding, value, at, old_recorded=T0):
    """The earlier goal record was wrong: supersede it at `at` and reissue with the same start."""
    old = f.fluent_iri(binding, "goal", old_recorded)
    g = rdflib.Graph()
    f.add_goal(g, binding, value, old_recorded, at)
    f.supersede(g, old, at)
    fx.post(UPDATE_URL, data=f.guarded_insert(g, [old]).encode("utf-8"))


GOALS_ONLY = {**ASK, "classes": ["goals"]}


def test_changed_goal_inside_the_window_makes_goals_ineligible_through_the_route(olt):
    binding = bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    replace_goal(olt.fx, binding, 80.0, NOW - timedelta(seconds=100), T0)
    out = assure(GOALS_ONLY).json()
    assert reasons(out, "goals") == {"result": "ineligible",
                                     "reasons": ["goal changed within window: LatencyMetric"]}


def test_changed_goal_outside_the_window_is_eligible(olt):
    binding = bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    replace_goal(olt.fx, binding, 80.0, NOW - timedelta(hours=1), T0)
    assert reasons(assure(GOALS_ONLY).json(), "goals")["result"] == "eligible"


def test_changed_goal_by_correction_is_not_a_change(olt):
    binding = bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    _correct_goal(olt.fx, binding, 90.0, NOW - timedelta(seconds=100))
    out = assure(GOALS_ONLY).json()
    assert reasons(out, "goals")["result"] == "eligible"
    assert [v["value"] for v in out["fluent_values"] if v["kind"] == "goal"] == [90.0]


def test_changed_goal_by_a_valid_from_reissue_inside_the_window_is_a_change(olt):
    binding = bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    _reissue(olt.fx, binding, 80.0, NOW - timedelta(seconds=100), NOW - timedelta(seconds=50))
    out = assure(GOALS_ONLY).json()
    assert reasons(out, "goals") == {"result": "ineligible",
                                     "reasons": ["goal changed within window: LatencyMetric"]}


def test_changed_goal_by_a_reissue_is_not_known_before_it_was_recorded(olt):
    binding = bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    _reissue(olt.fx, binding, 80.0, NOW - timedelta(seconds=100), NOW - timedelta(seconds=50))
    out = assure({**GOALS_ONLY, "known_at": "1m"}).json()
    assert reasons(out, "goals") == {"result": "eligible", "reasons": []}
    [goal] = [v for v in out["fluent_values"] if v["kind"] == "goal"]
    assert (goal["value"], goal["in_force"], goal["valid_to"]) == (100.0, True, None)


def test_changed_goal_in_a_past_request_judges_the_goal_in_force_then(olt):
    binding = bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    replace_goal(olt.fx, binding, 80.0, NOW - timedelta(minutes=30), T0)
    then = NOW - timedelta(hours=1)
    olt.vm.add("latency_ms", "olt1", "pon0", (90.0, (then - timedelta(seconds=30)).timestamp()))
    out = assure({**ASK, "as_of": "1h"}).json()
    assert reasons(out, "goals")["result"] == "eligible"
    assert [(v["value"], v["in_force"]) for v in out["fluent_values"] if v["kind"] == "goal"] == [
        (100.0, True)]  # the 80.0 goal started after the window, so it is not listed
    assert out["verdict"]["verdict"] == "success"  # 90 is inside the goal of 100, not the later 80
    olt.get_intent.assert_not_called()


def test_openapi_document_describes_the_contract_without_secrets_and_stays_public(rig):
    r = client.get("/openapi.json")  # no token
    assert r.status_code == 200
    spec = r.json()
    operation = spec["paths"]["/context/request"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ContextRequest")
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ContextResponse")
    schemas = spec["components"]["schemas"]
    request = schemas["ContextRequest"]["properties"]
    assert tuple(request["purpose"]["enum"]) == PURPOSES
    assert tuple(request["classes"]["items"]["enum"]) == CONTEXT_CLASSES
    [types] = [a for a in request["relationship_types"]["anyOf"] if a.get("type") == "array"]
    assert tuple(types["items"]["enum"]) == RELATIONSHIP_TYPES
    assert "grants no execution authority" in schemas["Eligibility"]["description"]
    assert "never reads eligibility" in schemas["ContextResponse"]["properties"]["verdict"]["description"]
    text = str(spec)
    for secret in ("Router-A", "olt1", "eyJ", "BEGIN", "test-caller", "testpassword0"):
        assert secret not in text
    rig.untouched()


# ---- the events class: stored syslog events for a device in a window (EVT-03) ----

EVENTS = {"purpose": "diagnosis", "scope": {"device": "Router-A"}, "classes": ["events"]}
EVENTS_AUTH = {**WIDE, "classes": ["topology", "events"]}
S = timedelta(seconds=1)
RAW_CANARY = "canary-raw-line-text-91"


def event(n, *, ingest=NOW - 59 * S, at=NOW - 60 * S, **kw):
    base = dict(event_id="evt-" + f"{n:032x}", ingest_time=ingest, event_time=at, severity=4,
                facility=3, format="rfc5424", source_host="Router-A", mapping="mapped",
                device="Router-A", candidates=("Router-A",), kind="link_down",
                raw=f"<28>1 {RAW_CANARY} {n}")
    base.update(kw)
    return NewEvent(**base)


def ambiguous(n, candidates=("Router-A", "router-a"), **kw):
    return event(n, mapping="ambiguous", device=None, candidates=candidates, kind=None,
                 source_host="rtr", **kw)


@pytest.fixture(autouse=True)
def _store_clock(monkeypatch):
    """The state file's retention sweep reads the store clock; fix it at NOW like the handler's, so the
    NOW-dated events here never age against the real date."""
    from broker_state import process
    real = process.StateStore
    monkeypatch.setattr(process, "StateStore", lambda *a, **kw: real(*a, clock=lambda: NOW, **kw))


def put(*events):
    get_store().add_events(list(events))


def events_headers(entities=("*",), classes=("topology", "events")):
    return token({"entities": list(entities), "purposes": ["diagnosis"], "classes": list(classes)})


def test_events_request_returns_the_stored_events_newest_first_with_flags(rig):
    put(event(1),
        event(2, ingest=NOW - 30 * S, at=None, format="rfc3164", kind=None, severity=6, facility=1))
    r = post(EVENTS, headers=events_headers())
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["events"]["window"] == {
        "start": (NOW - 900 * S).isoformat(), "end": NOW.isoformat(), "seconds": 900}
    assert out["events"]["cap"] == 100 and out["events"]["truncated"] is False
    newer, older = out["events"]["events"]
    assert newer == {
        "event_id": "evt-" + f"{2:032x}", "mapping": "mapped", "device": "Router-A",
        "source_host": "Router-A", "candidates": [], "candidates_withheld": 0, "format": "rfc3164",
        "facility": 1, "severity": 6, "event_time": None, "ingest_time": (NOW - 30 * S).isoformat(),
        "kind": None, "flags": ["time_unknown"],
        "provenance": {"source_system": "syslog", "reference": "evt-" + f"{2:032x}",
                       "observed_at": None, "retrieved_at": NOW.isoformat()}}
    assert older["kind"] == "link_down" and older["flags"] == []
    assert older["event_time"] == (NOW - 60 * S).isoformat()
    assert older["provenance"]["observed_at"] == (NOW - 60 * S).isoformat()
    assert out["eligibility"]["classes"] == {"events": {"result": "eligible", "reasons": []}}
    assert out["eligibility"]["result"] == "eligible"
    assert out["graph"] is None and out["fluent_values"] == [] and out["envelopes"] == []
    assert RAW_CANARY not in r.text and "<28>" not in r.text
    assert "message" not in str(out["events"]["events"][0].keys())


def test_an_event_stamped_after_its_ingest_time_is_flagged_future_dated(rig):
    put(event(1, ingest=NOW - 60 * S, at=NOW - 50 * S),   # 10 s after ingest
        event(2, ingest=NOW - 60 * S, at=NOW - 56 * S),   # 4 s: inside the tolerance
        event(3, ingest=NOW - 60 * S, at=NOW - 55 * S))   # exactly 5 s: inside
    flags = {e["event_id"][-1]: e["flags"]
             for e in post(EVENTS, headers=events_headers()).json()["events"]["events"]}
    assert flags == {"1": ["future_dated"], "2": [], "3": []}


def test_an_interface_scope_returns_the_events_of_its_device(rig):
    put(event(1))
    body = {**EVENTS, "scope": {"device": "Router-A", "interface": "GigabitEthernet0/0/0"}}
    out = post(body, headers=events_headers()).json()
    assert [e["event_id"] for e in out["events"]["events"]] == ["evt-" + f"{1:032x}"]


def test_events_of_another_device_are_not_returned(rig):
    put(event(1, device="Router-B", source_host="Router-B", candidates=("Router-B",)))
    out = post(EVENTS, headers=events_headers()).json()
    assert out["events"]["events"] == []


def test_no_event_in_the_window_is_a_gap(rig):
    out = post(EVENTS, headers=events_headers()).json()
    assert out["events"]["events"] == []
    assert out["eligibility"]["classes"]["events"] == {"result": "gap", "reasons": ["no events in window"]}
    assert out["eligibility"]["result"] == "gap"


def test_an_ambiguous_event_is_flagged_and_the_class_ineligible(rig):
    put(ambiguous(1))
    out = post(EVENTS, headers=events_headers(("*",))).json()
    [e] = out["events"]["events"]
    assert e["mapping"] == "ambiguous" and e["device"] is None and e["source_host"] == "rtr"
    assert e["candidates"] == ["Router-A", "router-a"] and e["candidates_withheld"] == 0
    assert "ambiguous" in e["flags"]
    assert out["eligibility"]["classes"]["events"] == {
        "result": "ineligible", "reasons": ["ambiguous identity: event source"]}
    assert out["eligibility"]["result"] == "ineligible"


def test_ambiguous_candidates_outside_the_callers_entities_are_only_counted(rig):
    put(ambiguous(1))
    r = post(EVENTS, headers=events_headers(("Router-A",)))
    [e] = r.json()["events"]["events"]
    assert e["candidates"] == ["Router-A"] and e["candidates_withheld"] == 1
    assert "router-a" not in r.text


def test_an_unmapped_event_is_in_no_response(rig):
    put(event(1, mapping="unmapped", device=None, candidates=(), source_host="edge-9", kind=None))
    r = post(EVENTS, headers=events_headers())
    assert r.json()["events"]["events"] == [] and "edge-9" not in r.text


def test_a_token_without_the_events_class_is_refused_before_anything_is_read(rig, monkeypatch):
    from broker_state.store import StateStore
    spy = Mock(side_effect=AssertionError("the store was read"))
    monkeypatch.setattr(StateStore, "events_in_window", spy)
    r = post(EVENTS, headers=token({**WIDE, "classes": ["topology"]}))
    assert r.status_code == 403 and r.json() == {"detail": "outside authority scope: class"}
    spy.assert_not_called()
    rig.untouched()
    assert post(TOPOLOGY, headers=token({**WIDE, "classes": ["topology"]})).status_code == 200


def test_an_events_only_request_reads_only_the_state_file(rig):
    put(event(1))
    r = post(EVENTS, headers=events_headers())
    assert r.status_code == 200
    rig.untouched()
    assert get_store().event_count() == 1


def test_events_with_topology_judge_each_class_and_keep_both_in_the_overall(rig):
    out = post({**TOPOLOGY, "classes": ["topology", "events"]}, headers=events_headers()).json()
    assert set(out["eligibility"]["classes"]) == {"topology", "events"}
    assert out["eligibility"]["classes"]["topology"]["result"] == "eligible"
    assert out["eligibility"]["classes"]["events"]["result"] == "gap"
    assert out["eligibility"]["result"] == "gap"
    assert out["graph"] is not None


def test_a_source_host_outside_the_token_pattern_is_returned_as_null(rig):
    put(event(1, source_host="a;b"))
    [e] = post(EVENTS, headers=events_headers()).json()["events"]["events"]
    assert e["source_host"] is None


# ---- the events window, the past, retention and the cap ----

def test_window_seconds_narrows_the_window_and_is_shown_in_the_situation(rig):
    put(event(1, at=NOW - 30 * S, ingest=NOW - 29 * S), event(2, at=NOW - 120 * S, ingest=NOW - 119 * S))
    out = post({**EVENTS, "window_seconds": 60}, headers=events_headers()).json()
    assert [e["event_id"][-1] for e in out["events"]["events"]] == ["1"]
    assert out["events"]["window"] == {
        "start": (NOW - 60 * S).isoformat(), "end": NOW.isoformat(), "seconds": 60}
    assert out["situation"]["window_seconds"] == 60
    assert post(EVENTS, headers=events_headers()).json()["situation"]["window_seconds"] == 900
    assert post(TOPOLOGY).json()["situation"]["window_seconds"] is None


def test_the_events_situation_id_is_built_with_the_resolved_window(rig):
    out = post({**EVENTS, "window_seconds": 60}, headers=events_headers()).json()
    expected = situation_id(
        requester="test-caller", purpose="diagnosis", device="Router-A", interface=None, intent_id=None,
        valid_at=NOW, known_at=None, classes=["events"], max_depth=None, relationship_types=None,
        window_seconds=60)
    assert out["situation"]["id"] == expected
    default = post(EVENTS, headers=events_headers()).json()["situation"]["id"]
    assert default == post({**EVENTS, "window_seconds": 900}, headers=events_headers()).json()["situation"]["id"]
    assert default != expected


def test_a_window_above_the_maximum_is_a_fixed_400_before_any_read(rig, monkeypatch):
    from broker_state.store import StateStore
    monkeypatch.setenv("BROKER_EVENT_WINDOW_MAX", "1h")
    spy = Mock(side_effect=AssertionError("the store was read"))
    monkeypatch.setattr(StateStore, "events_in_window", spy)
    r = post({**EVENTS, "window_seconds": 3601}, headers=events_headers())
    assert r.status_code == 400 and r.json() == {"detail": "window_seconds above the event window maximum"}
    spy.assert_not_called()
    rig.untouched()


def test_a_window_equal_to_the_maximum_is_answered(rig, monkeypatch):
    monkeypatch.setenv("BROKER_EVENT_WINDOW_MAX", "1h")
    r = post({**EVENTS, "window_seconds": 3600}, headers=events_headers())
    assert r.status_code == 200 and r.json()["events"]["window"]["seconds"] == 3600


@pytest.mark.parametrize("value", [0, -1, 604801, 1.5, "60", True])
def test_a_malformed_window_is_422_and_reaches_nothing(value, rig):
    r = post({**EVENTS, "window_seconds": value}, headers=events_headers())
    assert r.status_code == 422
    rig.untouched()


def test_window_seconds_without_the_events_class_is_422(rig):
    r = post({**TOPOLOGY, "window_seconds": 60}, headers=events_headers())
    assert r.status_code == 422 and "window_seconds needs the events class" in r.text
    rig.untouched()


def test_a_window_needs_the_events_class_in_the_token_too(rig):
    r = post({**EVENTS, "window_seconds": 60}, headers=token({**WIDE, "classes": ["topology"]}))
    assert r.status_code == 403 and r.json() == {"detail": "outside authority scope: class"}


def test_as_of_ends_the_window_in_the_past_and_the_answer_is_historical(rig):
    put(event(1, at=NOW - 2 * 3600 * S - 60 * S, ingest=NOW - 2 * 3600 * S - 59 * S),
        event(2, at=NOW - 60 * S, ingest=NOW - 59 * S))
    out = post({**EVENTS, "as_of": "2h"}, headers=events_headers()).json()
    assert out["events"]["window"]["end"] == (NOW - 2 * 3600 * S).isoformat()
    assert [e["event_id"][-1] for e in out["events"]["events"]] == ["1"]
    assert out["situation"]["historical"] is True and out["expiry_basis"] == "historical_request"
    assert out["expires_at"] == NOW.isoformat()


def test_known_at_excludes_events_ingested_after_it(rig):
    put(event(1, at=NOW - 5 * 60 * S, ingest=NOW - 5 * 60 * S),
        event(2, at=NOW - 15 * 60 * S, ingest=NOW - 15 * 60 * S))
    out = post({**EVENTS, "known_at": "10m", "window_seconds": 3600}, headers=events_headers()).json()
    assert [e["event_id"][-1] for e in out["events"]["events"]] == ["2"]
    assert out["expiry_basis"] == "historical_request"


@pytest.mark.parametrize("with_events", [False, True])
def test_a_window_reaching_beyond_the_event_retention_is_a_gap(with_events, rig, monkeypatch):
    monkeypatch.setenv("BROKER_EVENT_RETENTION", "1d")
    monkeypatch.setenv("BROKER_EVENT_WINDOW_MAX", "1d")
    from broker_state.process import reset_store
    reset_store()
    if with_events:
        put(event(1, at=NOW - 2 * 86400 * S - 5 * S, ingest=NOW - 2 * 86400 * S))
        put(event(2, at=NOW - 3600 * S, ingest=NOW - 3600 * S))
    out = post({**EVENTS, "as_of": "2d"}, headers=events_headers()).json()
    reasons = out["eligibility"]["classes"]["events"]["reasons"]
    assert "window reaches beyond event retention" in reasons
    assert out["eligibility"]["classes"]["events"]["result"] == "gap"


def test_a_window_inside_the_retention_has_no_retention_reason(rig):
    out = post(EVENTS, headers=events_headers()).json()
    assert "window reaches beyond event retention" not in out["eligibility"]["classes"]["events"]["reasons"]


def test_more_than_the_cap_is_truncated_to_the_newest_and_eligibility_is_unaffected(rig):
    put(*[event(n, at=NOW - n * S, ingest=NOW - n * S + S // 2) for n in range(1, 102)])
    out = post(EVENTS, headers=events_headers()).json()
    ids = [e["event_id"] for e in out["events"]["events"]]
    assert len(ids) == 100 and out["events"]["truncated"] is True and out["events"]["cap"] == 100
    assert ids[0] == "evt-" + f"{1:032x}" and ids[-1] == "evt-" + f"{100:032x}"
    assert out["eligibility"]["classes"]["events"] == {"result": "eligible", "reasons": []}


def test_exactly_the_cap_is_not_truncated(rig):
    put(*[event(n, at=NOW - n * S, ingest=NOW - n * S + S // 2) for n in range(1, 101)])
    out = post(EVENTS, headers=events_headers()).json()
    assert len(out["events"]["events"]) == 100 and out["events"]["truncated"] is False


def test_a_current_events_answer_with_an_event_expires_a_limit_after_retrieval(rig):
    put(event(1))
    out = post(EVENTS, headers=events_headers()).json()
    assert out["expires_at"] == (NOW + timedelta(seconds=300)).isoformat()
    assert out["expiry_basis"] == "event_window"
    assert post({**EVENTS, "freshness": "1m"}, headers=events_headers()).json()["expires_at"] == (
        NOW + timedelta(seconds=60)).isoformat()


def test_a_current_events_answer_with_no_event_has_no_evidence_expiry(rig):
    out = post(EVENTS, headers=events_headers()).json()
    assert out["expires_at"] == NOW.isoformat() and out["expiry_basis"] == "no_evidence"


# ---- every coherent combination of classes keeps one result per requested class ----

ALL_FOUR = {"entities": ["*"], "purposes": ["assurance"], "classes": list(CONTEXT_CLASSES)}
OLT_EVENT = dict(device="olt1", source_host="olt1", candidates=("olt1",))
COMBINATIONS = {
    "events": {"classes": ["events"]},
    "events_topology": {"classes": ["events", "topology"], "max_depth": 1, "relationship_types": BOTH},
    "events_goals": {"classes": ["events", "goals"], "intent_id": "i1"},
    "events_goals_observations": {"classes": ["events", "goals", "observations"], "intent_id": "i1"},
}


@pytest.mark.parametrize("name", sorted(COMBINATIONS))
def test_the_eligibility_has_exactly_one_entry_per_requested_class(name, olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    live_goal(olt)
    put(event(1, **OLT_EVENT))
    body = {"purpose": "assurance", "scope": OLT_PON0, **COMBINATIONS[name]}
    r = assure(body, authority=ALL_FOUR)
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out["eligibility"]["classes"]) == set(body["classes"])
    assert out["events"] is not None and out["events"]["events"][0]["device"] == "olt1"
    assert out["eligibility"]["classes"]["events"]["result"] == "eligible"
    # The overall result is the worst of the classes asked for, events included.
    results = {c["result"] for c in out["eligibility"]["classes"].values()}
    assert out["eligibility"]["result"] == ("ineligible" if "ineligible" in results
                                            else "gap" if "gap" in results else "eligible")


def test_an_ineligible_events_class_makes_the_overall_result_ineligible(rig):
    put(ambiguous(1))
    out = post({**TOPOLOGY, "classes": ["topology", "events"]}, headers=events_headers()).json()
    assert out["eligibility"]["classes"]["topology"]["result"] == "eligible"
    assert out["eligibility"]["classes"]["events"]["result"] == "ineligible"
    assert out["eligibility"]["result"] == "ineligible"


def test_a_request_without_the_events_class_has_no_events_key_value(rig):
    out = post(TOPOLOGY).json()
    assert out["events"] is None and "events" not in out["eligibility"]["classes"]


def test_the_openapi_document_lists_events_and_describes_its_result_without_names(rig):
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    assert "events" in schemas["ContextRequest"]["properties"]["classes"]["items"]["enum"]
    assert {"EventsResult", "EventOut", "EventWindow"} <= set(schemas)
    assert "window_seconds" in schemas["ContextRequest"]["properties"]
    assert "events" in schemas["ContextResponse"]["properties"]
    text = str(spec)
    for secret in ("Router-A", "olt1", "eyJ", "BEGIN", "test-caller", "testpassword0"):
        assert secret not in text
