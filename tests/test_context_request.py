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
from data_contracts import fluents as f
from data_contracts.context_contract import situation_id
from intent_management import tmf921_source
from tests.auth_helpers import make_token
from tests.fluent_helpers import IF0, IF1, IFACES_TTL, LAT, NS, T0, T1, GraphFuseki, bind, replace_goal

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
        assert tmf921_source._MARKS == {}


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
        "known_at": None, "retrieved_at": NOW.isoformat(), "historical": False}
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
    assert tmf921_source._MARKS == {}
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
    assert tmf921_source._MARKS == {}


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
