"""The knowledge class through POST /context/request (plan 06-06, OKF-01 to OKF-04).

The tracked `knowledge/` directory, an in-memory Qdrant and the fake embedder stand in for the real
service. The route, the token and the situation store are the real ones.
"""
import json
import re
from unittest.mock import patch

import pytest

from broker_state.process import get_store
from okf_retrieval.service import KnowledgeService, set_service
from tests.okf_helpers import FakeEmbedder, closed_client, local_client
from tests.test_context_request import NOW, Upstreams, post, rig, token  # noqa: F401
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
