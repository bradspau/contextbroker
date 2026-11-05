"""POST /context/request end to end: a minted token, the real route, the real SPARQL readers over the
rdflib stand-in tests.fluent_helpers.GraphFuseki, and a fake VictoriaMetrics.

The tracked demo topology (ontologies/abox/topology-demo.ttl) is the topology fixture. Every refusal
test asserts that neither Fuseki nor VictoriaMetrics nor the TMF921 client was touched. Times are fixed:
NOW is the one clock read of the handler.
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import rdflib
import requests
from fastapi.testclient import TestClient

from broker_engine import main
from data_contracts.context_contract import situation_id
from tests.auth_helpers import make_token
from tests.fluent_helpers import GraphFuseki

DEMO = Path(__file__).resolve().parent.parent / "ontologies" / "abox" / "topology-demo.ttl"
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
BOTH = ["has_interface", "connected_to"]
TOPOLOGY = {"purpose": "diagnosis", "scope": {"device": "Router-A"}, "classes": ["topology"],
            "max_depth": 3, "relationship_types": BOTH}
WIDE = {"entities": ["*"], "purposes": ["diagnosis"], "classes": ["topology"]}
A_IF = "Router-A/GigabitEthernet0/0/0"
B_IF = "Router-B/GigabitEthernet0/0/1"

client = TestClient(main.app)


class Upstreams:
    """What the route reached: Fuseki (the stand-in), VictoriaMetrics, and the TMF921 client."""

    def __init__(self, fx, get, get_intent, push, fulfilment):
        self.fx, self.get, self.get_intent, self.push, self.fulfilment = fx, get, get_intent, push, fulfilment

    def untouched(self):
        assert self.fx.posts == []
        self.get.assert_not_called()
        self.get_intent.assert_not_called()
        self.push.assert_not_called()
        self.fulfilment.assert_not_called()


def demo_graph():
    g = rdflib.Graph()
    g.parse(DEMO, format="turtle")
    return g


@pytest.fixture
def rig(monkeypatch):
    """The demo topology in the Fuseki stand-in; every upstream spied; the clock fixed at NOW."""
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "testpassword0")
    fx = GraphFuseki(demo_graph())
    with patch("requests.post", fx.post), patch("requests.get") as get, \
            patch("broker_engine.context_request._utcnow", return_value=NOW), \
            patch("intent_management.tmf921_client.Tmf921Client.get_intent") as get_intent, \
            patch("intent_management.tmf921_client.Tmf921Client.post_observation") as push, \
            patch("intent_management.tmf921_source.TMF921_STORE.fulfilment_update") as fulfilment:
        yield Upstreams(fx, get, get_intent, push, fulfilment)


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


# ---- classes not served yet (removed with the goals and observations classes) ----

def test_goals_and_observations_are_refused_until_served(rig):
    r = post({"purpose": "diagnosis", "scope": {"device": "olt1"}, "intent_id": "i1",
              "classes": ["goals", "observations"]},
             headers=token({**WIDE, "classes": ["goals", "observations"]}, scopes=("context:read", "context:assess")))
    assert r.status_code == 400
    assert r.json() == {"detail": "context class not served"}
    rig.untouched()
