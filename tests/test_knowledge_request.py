"""The knowledge class through POST /context/request (plan 06-06, OKF-01 to OKF-04).

The tracked `knowledge/` directory, an in-memory Qdrant and the fake embedder stand in for the real
service. The route, the token and the situation store are the real ones.
"""
import json
from datetime import timedelta
import re
from unittest.mock import patch

import pytest

from broker_state.process import get_store
from okf_retrieval.service import KnowledgeService, set_service
from tests.fluent_helpers import IF0, LAT, T0, bind
from tests.okf_helpers import FakeEmbedder, closed_client, local_client, write_bundle, write_catalogue
from tests.test_context_request import (  # noqa: F401
    ASK, BOTH, NOW, OLT_PON0, SCOPES, TOPOLOGY, client, live_goal, olt, post, rig, sample, token)
from tests.test_knowledge_bundles import KNOWLEDGE_ROOT

WIDE = {"entities": ["*"], "purposes": ["diagnosis"], "classes": ["knowledge"]}
INTERFACE = {"purpose": "diagnosis", "scope": {"device": "Router-A", "interface": "GigabitEthernet0/0/0"},
             "classes": ["knowledge"]}
DEVICE = {"purpose": "diagnosis", "scope": {"device": "Router-A"}, "classes": ["knowledge"]}
REAL = {"interface-error-handling", "latency-goal-investigation", "topology-change-check"}
REFERENCE_KEYS = {"identifier", "version", "status", "title", "content_hash", "matched_scope",
                  "source_references", "retrieval_rank", "linked_actions", "unresolved_actions",
                  "provenance"}


def serve(root=KNOWLEDGE_ROOT, client_factory=local_client, embedder_factory=FakeEmbedder):
    service = KnowledgeService(root=root, client_factory=client_factory, embedder_factory=embedder_factory)
    set_service(service)
    return service


@pytest.fixture
def served():
    return serve()


def ask(body=INTERFACE, authority=WIDE, **kw):
    return post(body, headers=token(authority, **kw))


# ---- the tracer: a token with the class asks about a Router-A interface ----

def test_an_interface_request_returns_the_three_real_drafts_as_references(rig, served):
    r = ask()
    assert r.status_code == 200, r.text
    out = r.json()
    knowledge = out["knowledge"]
    assert knowledge["retrieval"] == "answered" and knowledge["cap"] == 10
    assert knowledge["truncated"] is False and knowledge["withheld"] == 0
    refs = knowledge["references"]
    assert {ref["identifier"] for ref in refs} == REAL
    assert [ref["retrieval_rank"] for ref in refs] == [1, 2, 3]
    for ref in refs:
        assert set(ref) == REFERENCE_KEYS
        assert ref["version"] == "1.0" and ref["status"] == "draft" and ref["title"]
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", ref["content_hash"])
        assert ref["matched_scope"] == {"entities": ["Router-*"], "purpose": "diagnosis",
                                        "ontology_class": "Interface"}
        assert ref["source_references"]
        assert ref["provenance"] == {"source_system": "okf", "reference": f"{ref['identifier']}@1.0",
                                     "observed_at": None, "retrieved_at": NOW.isoformat()}
    assert out["eligibility"]["classes"]["knowledge"] == {"result": "eligible", "reasons": []}
    assert out["eligibility"]["result"] == "eligible"
    assert out["graph"] is None and out["verdict"] is None and out["events"] is None
    assert "out-of-scope-decoy" not in r.text and "Switch-Decoy-9" not in r.text


def test_the_interface_error_reference_names_the_approved_example_action(rig, served):
    refs = {ref["identifier"]: ref for ref in ask().json()["knowledge"]["references"]}
    errors = refs["interface-error-handling"]
    assert errors["linked_actions"] == [
        {"template_id": "example-collect-interface-counters", "version": "1.0", "approved": True}]
    assert errors["unresolved_actions"] == 0
    for name in REAL - {"interface-error-handling"}:
        assert refs[name]["linked_actions"] == [] and refs[name]["unresolved_actions"] == 0


def test_a_device_only_scope_returns_the_topology_check_alone(rig, served):
    refs = ask(DEVICE).json()["knowledge"]["references"]
    assert [ref["identifier"] for ref in refs] == ["topology-change-check"]
    assert refs[0]["matched_scope"]["ontology_class"] == "Device"


def test_a_knowledge_only_request_reaches_no_upstream(rig, served):
    assert ask().status_code == 200
    rig.untouched()


def test_the_request_records_one_situation_row(rig, served):
    before = get_store().situation_count()
    assert ask().status_code == 200
    assert get_store().situation_count() == before + 1


# ---- the class is refused before anything is retrieved ----

def test_a_token_without_the_class_is_refused_before_retrieval(rig, served):
    before = get_store().situation_count()
    with patch.object(KnowledgeService, "retrieve") as retrieve:
        r = ask(authority={**WIDE, "classes": ["topology"]})
    assert r.status_code == 403 and r.json() == {"detail": "outside authority scope: class"}
    retrieve.assert_not_called()
    assert get_store().situation_count() == before
    rig.untouched()


def test_the_same_token_still_answers_a_topology_request(rig, served):
    narrow = {**WIDE, "classes": ["topology"]}
    topology = {"purpose": "diagnosis", "scope": {"device": "Router-A"}, "classes": ["topology"],
                "max_depth": 1, "relationship_types": ["has_interface"]}
    assert ask(topology, narrow).status_code == 200


# ---- the response never carries a body, an excerpt or a ranking value ----

def test_no_bundle_text_and_no_ranking_value_reaches_the_response(rig, served):
    text = ask().text
    for path in KNOWLEDGE_ROOT.glob("bundles/*/*.md"):
        body = path.read_text().split("---\n", 2)[2].strip()
        assert body[:40] not in text

    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    assert not {"score", "similarity", "body", "excerpt", "content"} & set(keys(json.loads(text)))


# ---- retrieval cannot answer: an honest gap, and nothing else changes ----

UNAVAILABLE_GAP = {"result": "gap", "reasons": ["knowledge retrieval unavailable"]}
TOPOLOGY_KNOWLEDGE = {**TOPOLOGY, "classes": ["topology", "knowledge"]}
BOTH_CLASSES = {"entities": ["*"], "purposes": ["diagnosis"], "classes": ["topology", "knowledge"]}


def test_an_unavailable_store_is_a_200_with_a_gap_and_no_references(rig):
    serve(client_factory=closed_client)
    r = ask()
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["knowledge"]["retrieval"] == "unavailable" and out["knowledge"]["references"] == []
    assert out["knowledge"]["withheld"] == 0 and out["knowledge"]["truncated"] is False
    assert out["eligibility"]["classes"]["knowledge"] == UNAVAILABLE_GAP
    assert out["expiry_basis"] == "no_evidence"


def test_an_unavailable_store_leaves_the_topology_answer_as_it_was(rig):
    topology = post(TOPOLOGY, headers=token({**BOTH_CLASSES, "classes": ["topology"]})).json()
    serve(client_factory=closed_client)
    both = ask(TOPOLOGY_KNOWLEDGE, BOTH_CLASSES).json()
    assert both["graph"] == topology["graph"]
    assert both["fluent_values"] == topology["fluent_values"]
    assert both["eligibility"]["classes"]["topology"] == topology["eligibility"]["classes"]["topology"]
    assert both["eligibility"]["classes"]["knowledge"] == UNAVAILABLE_GAP
    assert both["eligibility"]["result"] == "gap"


def test_a_failing_embedder_is_the_same_unavailable_answer(rig):
    def broken():
        raise RuntimeError("model missing")
    serve(embedder_factory=broken)
    assert ask().json()["eligibility"]["classes"]["knowledge"] == UNAVAILABLE_GAP


# ---- the verdict never reads the knowledge class ----

OLT_TREE_ID = "olt-interface-check"
ASSURE_KNOWLEDGE = {"entities": ["*"], "purposes": ["assurance"],
                    "classes": ["topology", "goals", "observations", "knowledge"]}


def olt_tree(tmp_path):
    write_bundle(tmp_path, OLT_TREE_ID, entities=("olt1",), purposes=("assurance",),
                 ontology_classes=("Interface",))
    write_catalogue(tmp_path, [])
    return tmp_path


def assess(olt, body):
    return post(body, headers=token(ASSURE_KNOWLEDGE, scopes=SCOPES))


@pytest.mark.parametrize("mode", ["unavailable", "answering"])
def test_the_verdict_is_identical_with_and_without_the_knowledge_class(mode, olt, tmp_path):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    live_goal(olt)
    if mode == "unavailable":
        serve(client_factory=closed_client)
    else:
        serve(root=olt_tree(tmp_path))
    plain = assess(olt, ASK).json()
    with_knowledge = assess(olt, {**ASK, "classes": ["goals", "observations", "knowledge"]}).json()
    assert plain["verdict"]["verdict"] == "success"
    assert with_knowledge["verdict"] == plain["verdict"]
    assert with_knowledge["fluent_values"] == plain["fluent_values"]
    assert with_knowledge["envelopes"] == plain["envelopes"]
    for name in ("goals", "observations"):
        assert with_knowledge["eligibility"]["classes"][name] == plain["eligibility"]["classes"][name]
    wanted = {"unavailable": "unavailable", "answering": "answered"}[mode]
    assert with_knowledge["knowledge"]["retrieval"] == wanted
    if mode == "answering":
        assert [r["identifier"] for r in with_knowledge["knowledge"]["references"]] == [OLT_TREE_ID]


# ---- a question about the past is a gap and retrieves nothing ----

@pytest.mark.parametrize("past", [{"as_of": "10m"}, {"known_at": "10m"}])
def test_a_past_request_is_a_current_only_gap_without_retrieval(past, rig, served):
    with patch.object(KnowledgeService, "retrieve") as retrieve:
        r = ask({**INTERFACE, **past})
    assert r.status_code == 200, r.text
    out = r.json()
    retrieve.assert_not_called()
    assert out["situation"]["historical"] is True
    assert out["knowledge"] == {"retrieval": "not_current", "cap": 10, "truncated": False,
                                "references": [], "withheld": 0}
    assert out["eligibility"]["classes"]["knowledge"] == {
        "result": "gap", "reasons": ["knowledge is current only"]}


def test_a_request_without_a_token_is_401_and_retrieves_nothing(rig, served):
    with patch.object(KnowledgeService, "retrieve") as retrieve:
        r = client.post("/context/request", json=INTERFACE)
    assert r.status_code == 401
    retrieve.assert_not_called()


# ---- authority over entities: withheld is a count ----

def test_a_token_for_router_a_only_sees_a_withheld_count_and_no_decoy_name(rig, served):
    r = ask(DEVICE, {**WIDE, "entities": ["Router-A"]})
    out = r.json()
    assert r.status_code == 200
    assert out["knowledge"]["withheld"] == 1
    assert "Switch-Decoy-9" not in r.text and "out-of-scope-decoy" not in r.text
    wide = ask(DEVICE).json()
    assert wide["knowledge"]["withheld"] == 0


# ---- the cap ----

def test_twelve_applicable_bundles_give_ten_references_and_stay_eligible(rig, tmp_path):
    for n in range(12):
        write_bundle(tmp_path, f"router-note-{n:02d}", purposes=("diagnosis",))
    write_catalogue(tmp_path, [])
    serve(root=tmp_path)
    out = ask(DEVICE).json()
    assert len(out["knowledge"]["references"]) == 10
    assert out["knowledge"]["truncated"] is True
    assert [r["retrieval_rank"] for r in out["knowledge"]["references"]] == list(range(1, 11))
    assert out["eligibility"]["classes"]["knowledge"] == {"result": "eligible", "reasons": []}


def test_no_applicable_artefact_is_a_gap(rig, served):
    out = ask({**DEVICE, "scope": {"device": "Switch-Z"}}).json()
    assert out["knowledge"]["retrieval"] == "answered" and out["knowledge"]["references"] == []
    assert out["eligibility"]["classes"]["knowledge"] == {
        "result": "gap", "reasons": ["no applicable knowledge"]}


# ---- every coherent combination keeps one result per requested class ----

ALL_CLASSES = {"entities": ["*"], "purposes": ["assurance"],
               "classes": ["topology", "goals", "observations", "events", "knowledge"]}
COMBINATIONS = {
    "knowledge": {"classes": ["knowledge"]},
    "knowledge_topology": {"classes": ["knowledge", "topology"], "max_depth": 1, "relationship_types": BOTH},
    "knowledge_events": {"classes": ["knowledge", "events"]},
    "knowledge_goals_observations": {"classes": ["knowledge", "goals", "observations"], "intent_id": "i1"},
}


@pytest.mark.parametrize("name", sorted(COMBINATIONS))
def test_the_eligibility_has_one_entry_per_requested_class(name, olt, tmp_path):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    live_goal(olt)
    serve(root=olt_tree(tmp_path))
    body = {"purpose": "assurance", "scope": OLT_PON0, **COMBINATIONS[name]}
    r = post(body, headers=token(ALL_CLASSES, scopes=SCOPES))
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out["eligibility"]["classes"]) == set(body["classes"])
    assert out["eligibility"]["classes"]["knowledge"] == {"result": "eligible", "reasons": []}
    assert out["knowledge"]["references"][0]["identifier"] == OLT_TREE_ID


# ---- the topic steers retrieval and is its own situation ----

class SpyEmbedder(FakeEmbedder):
    queries = []

    def embed_query(self, text):
        SpyEmbedder.queries.append(text)
        return super().embed_query(text)


def test_the_topic_is_the_retrieval_query_and_is_shown_in_the_situation(rig):
    SpyEmbedder.queries = []
    serve(embedder_factory=SpyEmbedder)
    topic = "interface errors on the router interface"
    out = ask({**INTERFACE, "topic": topic}).json()
    assert SpyEmbedder.queries == [topic]
    assert out["situation"]["topic"] == topic
    assert {ref["identifier"] for ref in out["knowledge"]["references"]} == REAL
    assert ask().json()["situation"]["topic"] is None
    assert SpyEmbedder.queries[-1] == "diagnosis interface"


def test_each_topic_is_its_own_situation(rig, served):
    plain = ask().json()["situation"]["id"]
    first = ask({**INTERFACE, "topic": "interface errors"}).json()["situation"]["id"]
    second = ask({**INTERFACE, "topic": "latency goal"}).json()["situation"]["id"]
    assert len({plain, first, second}) == 3
    assert ask({**INTERFACE, "topic": "interface errors"}).json()["situation"]["id"] == first
    assert get_store().situation_count() == 3


def test_a_topic_without_the_knowledge_class_is_422(rig, served):
    topology = {"purpose": "diagnosis", "scope": {"device": "Router-A"}, "classes": ["topology"],
                "max_depth": 1, "relationship_types": ["has_interface"], "topic": "interface errors"}
    r = ask(topology, {**WIDE, "classes": ["topology"]})
    assert r.status_code == 422 and "topic needs the knowledge class" in r.text


@pytest.mark.parametrize("topic", ["", "a" * 201, "a;b", "a\nb", 5, True])
def test_a_hostile_topic_is_422_and_retrieves_nothing(topic, rig, served):
    with patch.object(KnowledgeService, "retrieve") as retrieve:
        r = ask({**INTERFACE, "topic": topic})
    assert r.status_code == 422
    retrieve.assert_not_called()
    assert get_store().situation_count() == 0


# ---- expiry ----

def test_a_current_answer_with_references_expires_a_limit_after_retrieval(rig, served):
    out = ask().json()
    assert out["expiry_basis"] == "knowledge_set"
    assert out["expires_at"] == (NOW + timedelta(seconds=300)).isoformat()
    short = ask({**INTERFACE, "freshness": "1m"}).json()
    assert short["expires_at"] == (NOW + timedelta(seconds=60)).isoformat()


def test_an_empty_answer_has_no_evidence_expiry(rig, served):
    out = ask({**DEVICE, "scope": {"device": "Switch-Z"}}).json()
    assert out["expiry_basis"] == "no_evidence" and out["expires_at"] == NOW.isoformat()


def test_an_answer_with_an_observation_expires_at_the_oldest_observation(olt, tmp_path):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt, age_s=30)
    live_goal(olt)
    serve(root=olt_tree(tmp_path))
    body = {**ASK, "classes": ["goals", "observations", "knowledge"]}
    out = post(body, headers=token(ASSURE_KNOWLEDGE, scopes=SCOPES)).json()
    assert out["expiry_basis"] == "oldest_observation"


def test_an_answer_with_events_keeps_the_event_window_basis(rig, served):
    from tests.test_context_request import event, put
    put(event(1))
    body = {"purpose": "diagnosis", "scope": {"device": "Router-A"}, "classes": ["events", "knowledge"]}
    out = ask(body, {**WIDE, "classes": ["events", "knowledge"]}).json()
    assert out["knowledge"]["references"] and out["events"]["events"]
    assert out["expiry_basis"] == "event_window"
