"""Promoted state through POST /context/request (plan 07-05, SEM-01, D-60 to D-67).

Rows are seeded through the state store here; the operator command (plans 07-04 and 07-07) writes them
for real. A promoted record is read from the state file only and is never evidence: every test that
compares an answer with and without rows asks the same question twice.
"""
import copy

import pytest
import rdflib

from broker_state.process import get_store
from tests.promotion_helpers import EVENT, SID, new_hypothesis, new_promoted
from tests.test_context_request import (  # noqa: F401
    A_IF, B_IF, EVENTS, NOW, S, TOPOLOGY, WIDE, _store_clock, _upstreams, client, demo_graph, event,
    events_headers, olt, post, put, rig, token)

PROMOTED_AT = NOW - 60 * S
VALID_FROM = NOW - 300 * S
REQUESTER = "test-caller"
CONFLICT_TEXT = "event link_down contradicts configured link_in_force"


def seed_events(*numbers):
    put(*[event(n) for n in numbers])


def ask_events(headers=None, body=None):
    r = post(body or EVENTS, headers=headers or events_headers())
    assert r.status_code == 200, r.text
    return r.json()


def promote_conflict(situation_id, n=1, requester=REQUESTER, event_ids=None, device="Router-A"):
    event_ids = (EVENT(1), EVENT(2), EVENT(3)) if event_ids is None else event_ids
    get_store().promote(
        [new_promoted(n, device=device, situation_id=situation_id, requester=requester,
                      event_ids=event_ids, valid_from=VALID_FROM)], now=PROMOTED_AT)


def without_promoted(out):
    """The answer with the one field under test removed."""
    out = copy.deepcopy(out)
    if out["events"] is not None:
        out["events"].pop("promoted_conflicts")
    return out


# ---- the tracer: a promoted conflict is delivered under the events class with its support ----

def test_a_promoted_conflict_is_returned_with_its_support_and_the_rest_of_the_answer_is_unchanged(rig):
    seed_events(1, 2, 3)
    first = ask_events()
    sid = first["situation"]["id"]
    promote_conflict(sid)
    second = ask_events()
    pid = "prm-" + f"{1:032x}"
    assert first["events"]["promoted_conflicts"] == []
    assert second["events"]["promoted_conflicts"] == [{
        "promoted_id": pid, "rule": "repeated_link_down_vs_configured", "device": "Router-A",
        "event": "link_down", "configured": "link_in_force", "text": CONFLICT_TEXT,
        "valid_from": VALID_FROM.isoformat(), "promoted_at": PROMOTED_AT.isoformat(),
        "support": [{"kind": "situation", "id": sid, "state": "present"}]
                   + [{"kind": "event", "id": EVENT(n), "state": "present"} for n in (1, 2, 3)],
        "provenance": {"source_system": "broker-promotion", "reference": pid,
                       "observed_at": PROMOTED_AT.isoformat(), "retrieved_at": NOW.isoformat()}}]
    assert without_promoted(first) == without_promoted(second)
    assert second["situation"]["id"] == first["situation"]["id"]
    assert rig.fx.updates() == []


def test_a_fetched_situation_points_at_the_promoted_record_it_delivered(rig):
    seed_events(1, 2, 3)
    sid = ask_events()["situation"]["id"]
    promote_conflict(sid)
    ask_events()
    r = client.get(f"/context/situation/{sid}", headers=events_headers())
    assert r.status_code == 200, r.text
    pid = "prm-" + f"{1:032x}"
    refs = [p for p in r.json()["evidence"]["provenance"] if p["source_system"] == "broker-promotion"]
    assert refs == [{"source_system": "broker-promotion", "reference": pid,
                     "observed_at": PROMOTED_AT.isoformat(), "retrieved_at": NOW.isoformat()}]


# ---- hypotheses: promoted links inferred from neighbour-discovery events, in the topology class ----

A1 = "Router-A/GigabitEthernet0/0/1"
C2 = "Router-C/GigabitEthernet0/0/2"
HYPOTHESIS_RULE = "adjacency_without_configured_link"
EXTRA = """
@prefix : <http://example.org/network-ontology#> .
:if-router-a-ge001 a :Interface ; :onDevice "Router-A" ; :interfaceName "GigabitEthernet0/0/1" .
:if-router-c-ge002 a :Interface ; :onDevice "Router-C" ; :interfaceName "GigabitEthernet0/0/2" .
"""


@pytest.fixture
def hyp(monkeypatch):
    """The demo topology plus two interfaces no link joins."""
    graph = demo_graph()
    graph.parse(data=EXTRA, format="turtle")
    yield from _upstreams(monkeypatch, graph)


def ask_topology(headers=None, body=None):
    r = post(body or TOPOLOGY, headers=headers or token())
    assert r.status_code == 200, r.text
    return r.json()


def promote_hypotheses(*records):
    get_store().promote(list(records), now=PROMOTED_AT)


def hypothesis(n=1, a=A1, b=C2, sid=SID, requester=REQUESTER):
    return new_hypothesis(n, a, b, situation_id=sid, requester=requester, valid_from=VALID_FROM)


def without_hypotheses(out):
    out = copy.deepcopy(out)
    for key in ("hypotheses", "hypotheses_truncated", "hypotheses_withheld"):
        out["graph"].pop(key)
    return out


def test_a_hypothesis_is_a_separate_inferred_edge_with_its_support_and_changes_nothing_else(hyp):
    put(event(1), event(2), event(3))
    first = ask_topology()
    sid = first["situation"]["id"]
    promote_hypotheses(hypothesis(sid=sid))
    second = ask_topology()
    pid = "prm-" + f"{1:032x}"
    assert first["graph"]["hypotheses"] == [] and first["graph"]["hypotheses_withheld"] == 0
    assert second["graph"]["hypotheses"] == [{
        "promoted_id": pid, "from": A1, "to": C2, "label": "hypothesis", "state": "inferred",
        "rule": HYPOTHESIS_RULE, "valid_from": VALID_FROM.isoformat(),
        "promoted_at": PROMOTED_AT.isoformat(),
        "support": [{"kind": "situation", "id": sid, "state": "present"}]
                   + [{"kind": "event", "id": EVENT(n), "state": "present"} for n in (1, 2, 3)],
        "provenance": {"source_system": "broker-promotion", "reference": pid,
                       "observed_at": PROMOTED_AT.isoformat(), "retrieved_at": NOW.isoformat()}}]
    assert second["graph"]["hypotheses_withheld"] == 0 and second["graph"]["hypotheses_truncated"] is False
    assert without_hypotheses(first) == without_hypotheses(second)
    assert "Router-C" not in [n["device"] for n in second["graph"]["nodes"]]
    assert all("Router-C" not in (e["from"] + e["to"]) for e in second["graph"]["edges"])
    assert second["eligibility"]["classes"]["topology"] == {"result": "eligible", "reasons": []}
    assert hyp.fx.updates() == []


def test_a_hypothesis_with_an_end_outside_the_callers_entities_is_only_counted(hyp):
    promote_hypotheses(hypothesis())
    narrow = {**WIDE, "entities": ["Router-A", "Router-B"]}
    r = post(TOPOLOGY, headers=token(narrow))
    out = r.json()
    assert r.status_code == 200
    assert out["graph"]["hypotheses"] == [] and out["graph"]["hypotheses_withheld"] == 1
    assert "Router-C" not in r.text and "router-c" not in r.text


def test_hypotheses_follow_the_connected_to_relationship_and_a_returned_node(hyp):
    promote_hypotheses(hypothesis())
    only_interfaces = ask_topology(body={**TOPOLOGY, "relationship_types": ["has_interface"]})
    assert only_interfaces["graph"]["hypotheses"] == []
    assert only_interfaces["graph"]["hypotheses_withheld"] == 0
    scoped = {**TOPOLOGY, "scope": {"device": "Router-A", "interface": "GigabitEthernet0/0/1"},
              "max_depth": 0}
    out = ask_topology(body=scoped)
    assert [n["id"] for n in out["graph"]["nodes"]] == [A1]
    assert [h["to"] for h in out["graph"]["hypotheses"]] == [C2]
    # A device whose nodes do not include either end has nothing to show.
    far = ask_topology(body={**TOPOLOGY, "scope": {"device": "Router-B"}, "max_depth": 0})
    assert far["graph"]["hypotheses"] == []


def test_a_hypothesis_over_a_configured_pair_a_later_valid_from_or_a_later_promotion_is_not_returned(hyp):
    promote_hypotheses(hypothesis(1, A_IF, B_IF))
    assert ask_topology()["graph"]["hypotheses"] == []
    promote_hypotheses(hypothesis(2))
    assert [h["promoted_id"] for h in ask_topology()["graph"]["hypotheses"]] == ["prm-" + f"{2:032x}"]
    before_valid_from = ask_topology(body={**TOPOLOGY, "as_of": "10m"})
    assert before_valid_from["graph"]["hypotheses"] == []
    before_promotion = ask_topology(body={**TOPOLOGY, "known_at": "2m"})
    assert before_promotion["graph"]["hypotheses"] == []


def test_more_than_fifty_hypotheses_are_cut_sorted_and_marked_truncated(hyp):
    promote_hypotheses(*[hypothesis(n, A1, f"Router-C/GigabitEthernet0/0/{n}") for n in range(1, 52)])
    graph = ask_topology()["graph"]
    ends = [(h["from"], h["to"], h["promoted_id"]) for h in graph["hypotheses"]]
    assert len(ends) == 50 and ends == sorted(ends)
    expected = sorted(f"Router-C/GigabitEthernet0/0/{n}" for n in range(1, 52))[:50]
    assert [e[1] for e in ends] == expected
    assert graph["hypotheses_truncated"] is True


def test_exactly_fifty_hypotheses_are_not_marked_truncated(hyp):
    promote_hypotheses(*[hypothesis(n, A1, f"Router-C/GigabitEthernet0/0/{n}") for n in range(1, 51)])
    graph = ask_topology()["graph"]
    assert len(graph["hypotheses"]) == 50 and graph["hypotheses_truncated"] is False


def test_a_stored_situation_lists_the_broker_promotion_record_of_a_returned_hypothesis(hyp):
    sid = ask_topology()["situation"]["id"]
    promote_hypotheses(hypothesis(sid=sid))
    ask_topology()
    r = client.get(f"/context/situation/{sid}", headers=token())
    assert r.status_code == 200, r.text
    refs = [p for p in r.json()["evidence"]["provenance"] if p["source_system"] == "broker-promotion"]
    assert [p["reference"] for p in refs] == ["prm-" + f"{1:032x}"]
    assert r.json()["evidence"]["graph_nodes"] == sorted(
        n["id"] for n in ask_topology()["graph"]["nodes"])


def test_a_request_without_the_topology_class_reads_no_hypothesis(rig, monkeypatch):
    from broker_state.store import StateStore
    seen = []
    real = StateStore.promoted_rows
    monkeypatch.setattr(StateStore, "promoted_rows",
                        lambda self, kind, *a, **kw: seen.append(kind) or real(self, kind, *a, **kw))
    seed_events(1)
    ask_events()
    assert seen == ["conflict"]
