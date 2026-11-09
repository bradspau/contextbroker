"""Promoted state through POST /context/request (plan 07-05, SEM-01, D-60 to D-67).

Rows are seeded through the state store here; the operator command (plans 07-04 and 07-07) writes them
for real. A promoted record is read from the state file only and is never evidence: every test that
compares an answer with and without rows asks the same question twice.
"""
import copy
import json
import logging
from datetime import timedelta
from unittest.mock import patch

import pytest

from broker_state.process import get_store
from broker_state.store import NewPromoted
from data_contracts import fluents as f
from tests.fluent_helpers import IF0, IF1, LAT, NS, T0, bind
from tests.promotion_helpers import EVENT, SID, new_hypothesis, new_promoted
from tests.test_context_request import (  # noqa: F401
    A_IF, ALL_FOUR, B_IF, BOTH, EVENTS, NOW, OLT_EVENT, OLT_PON0, RAW_CANARY, S, TOPOLOGY, WIDE,
    _store_clock, _upstreams, assure, client, demo_graph, event, events_headers, live_goal, olt, post,
    put, rig, sample, token)

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


# ---- a promoted record is never evidence (D-64) ----

def test_untouched_a_promoted_conflict_is_returned_even_with_no_event_in_the_window(rig):
    bare = ask_events()
    promote_conflict(SID, requester="someone-else")
    out = ask_events()
    assert [p["promoted_id"] for p in out["events"]["promoted_conflicts"]] == ["prm-" + f"{1:032x}"]
    assert out["events"]["events"] == []
    assert out["eligibility"]["classes"]["events"] == {"result": "gap", "reasons": ["no events in window"]}
    assert out["eligibility"]["result"] == "gap"
    assert (out["expires_at"], out["expiry_basis"]) == (bare["expires_at"], bare["expiry_basis"])
    assert without_promoted(out) == without_promoted(bare)
    rig.untouched()  # the state file only: no Fuseki query, no VictoriaMetrics, no TMF921 call


OLT_ALL = {"purpose": "assurance", "scope": OLT_PON0, "intent_id": "i1", "max_depth": 1,
           "relationship_types": BOTH, "classes": ["topology", "goals", "observations", "events"]}


def strip_promoted(out):
    out = copy.deepcopy(out)
    out["events"].pop("promoted_conflicts")
    for key in ("hypotheses", "hypotheses_truncated", "hypotheses_withheld"):
        out["graph"].pop(key)
    return out


def test_spy_the_verdict_eligibility_expiry_and_configured_side_are_identical_with_promoted_rows(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    live_goal(olt)
    f.add_link_fluent(olt.fx.graph, NS + "link-olt1-loop", IF0, IF1, T0, T0)
    put(event(1, **OLT_EVENT))
    judged = {"verdict": "success", "reasons": [], "context": [], "goals": []}
    results = {}
    triples, updates = len(olt.fx.graph), len(olt.fx.updates())
    for name in ("without", "with"):
        if name == "with":
            sid = results["without"][0]["situation"]["id"]
            get_store().promote([
                new_promoted(1, device="olt1", situation_id=sid, requester=REQUESTER,
                             valid_from=VALID_FROM),
                new_hypothesis(2, "olt1/pon0", "olt2/pon0", situation_id=sid, requester=REQUESTER,
                               valid_from=VALID_FROM)], now=PROMOTED_AT)
        before = len(olt.queries())
        vm_before = len(olt.vm.queries)
        with patch("broker_engine.context_request.assess_tmf921_as_of",
                   side_effect=lambda *a, **kw: dict(judged)) as spy:
            r = assure(OLT_ALL, authority=ALL_FOUR)
        assert r.status_code == 200, r.text
        [call] = spy.call_args_list
        results[name] = (r.json(), call, olt.queries()[before:], olt.vm.queries[vm_before:])
    plain, with_rows = results["without"], results["with"]
    assert with_rows[0]["events"]["promoted_conflicts"] and with_rows[0]["graph"]["hypotheses"]
    assert plain[0]["events"]["promoted_conflicts"] == [] and plain[0]["graph"]["hypotheses"] == []
    assert plain[0]["events"]["conflicts"]  # the live conflict is there in both
    def shape(call):  # the recording joiner is a fresh closure per request; everything else must match
        return call.args, {k: v for k, v in call.kwargs.items() if k != "join"}
    assert shape(with_rows[1]) == shape(plain[1])
    assert strip_promoted(with_rows[0]) == strip_promoted(plain[0])  # verdict, every eligibility class,
    # expires_at, expiry_basis, fluent values, graph nodes, edges and limits, configured and conflicts
    assert with_rows[0]["eligibility"] == plain[0]["eligibility"]
    assert with_rows[0]["verdict"] == plain[0]["verdict"] == {"verdict": "success", "reasons": []}
    assert with_rows[2] == plain[2] and with_rows[3] == plain[3]  # the same Fuseki and telemetry reads
    assert olt.fx.updates()[updates:] == [] and len(olt.fx.graph) == triples
    olt.push.assert_not_called()


def test_the_situation_id_and_stored_body_do_not_change_unless_a_promoted_record_is_returned(rig):
    seed_events(1, 2, 3)
    first = ask_events()
    sid = first["situation"]["id"]
    assert "broker-promotion" not in get_store().get_situation(sid, now=NOW).body
    promote_conflict(sid)
    second = ask_events()
    assert second["situation"]["id"] == sid
    assert "broker-promotion" in get_store().get_situation(sid, now=NOW).body


# ---- support: who sees a situation id, and what an eviction looks like (D-60, D-66) ----

def test_withheld_another_requester_sees_no_situation_id_but_the_event_ids(rig):
    seed_events(1, 2, 3)
    sid = ask_events()["situation"]["id"]
    promote_conflict(sid, requester=REQUESTER)
    other = token({"entities": ["*"], "purposes": ["diagnosis"], "classes": ["events"]}, sub="someone-else")
    r = post(EVENTS, headers=other)
    assert r.status_code == 200, r.text
    [record] = r.json()["events"]["promoted_conflicts"]
    assert record["support"][0] == {"kind": "situation", "id": None, "state": "withheld"}
    assert [s["id"] for s in record["support"][1:]] == [EVENT(1), EVENT(2), EVENT(3)]
    assert sid not in r.text
    assert ask_events()["events"]["promoted_conflicts"][0]["support"][0]["id"] == sid


def test_withheld_holds_for_a_support_situation_that_no_longer_exists(rig):
    promote_conflict(SID, requester="someone-else")  # no such situation was ever stored
    [record] = ask_events()["events"]["promoted_conflicts"]
    assert record["support"][0] == {"kind": "situation", "id": None, "state": "withheld"}
    assert [s["state"] for s in record["support"][1:]] == ["evicted"] * 3  # no event was stored either


def test_evicted_support_stays_visible_and_marked_past_its_own_retention_without_a_sweep(rig):
    seed_events(1, 2, 3)
    sid = ask_events()["situation"]["id"]
    promote_conflict(sid)
    store = get_store()
    later = NOW + timedelta(days=10)
    assert later > NOW + timedelta(seconds=store.event_retention_s + 1)
    assert later < PROMOTED_AT + timedelta(seconds=store.promoted_retention_s)
    with patch("broker_engine.context_request._utcnow", return_value=later):
        r = post(EVENTS, headers=events_headers())
    assert r.status_code == 200, r.text
    [record] = r.json()["events"]["promoted_conflicts"]
    assert record["support"] == [{"kind": "situation", "id": sid, "state": "evicted"}] + [
        {"kind": "event", "id": EVENT(n), "state": "evicted"} for n in (1, 2, 3)]
    assert r.json()["events"]["events"] == []


def test_a_promoted_record_past_its_own_retention_is_not_returned(rig):
    promote_conflict(SID)
    later = PROMOTED_AT + timedelta(seconds=get_store().promoted_retention_s + 1)
    with patch("broker_engine.context_request._utcnow", return_value=later):
        r = post(EVENTS, headers=events_headers())
    assert r.json()["events"]["promoted_conflicts"] == []


# ---- a malformed or hostile stored row never breaks or colours the answer ----

def promoted_with_body(body, n=1):
    return NewPromoted(promoted_id="prm-" + f"{n:032x}", kind="conflict",
                       rule="repeated_link_down_vs_configured", device_a="Router-A", device_b=None,
                       body=json.dumps(body), valid_from=VALID_FROM, situation_id=SID,
                       situation_requester=REQUESTER, event_ids=(EVENT(1),))


@pytest.mark.parametrize("body", [
    {"device": "Router-A", "event": "not_a_kind", "configured": "link_in_force", "text": "x"},
    {"device": "Router-A", "event": "link_down", "configured": "not_a_value", "text": "x"},
    {"device": "Router-B", "event": "link_down", "configured": "link_in_force", "text": "x"},
    {"event": "link_down", "configured": "link_in_force"},
])
def test_an_unreadable_promoted_row_is_skipped_with_one_fixed_line_naming_nothing(rig, caplog, body):
    get_store().promote([promoted_with_body(body)], now=PROMOTED_AT)
    with caplog.at_level(logging.DEBUG):
        r = post(EVENTS, headers=events_headers())
    assert r.status_code == 200, r.text
    assert r.json()["events"]["promoted_conflicts"] == []
    lines = [rec.getMessage() for rec in caplog.records if "promoted record" in rec.getMessage()]
    assert lines == ["promoted record unreadable"]
    assert "prm-" + f"{1:032x}" not in caplog.text and "Router-A" not in caplog.text


def test_a_stored_text_is_never_echoed_the_sentence_is_rebuilt_from_the_closed_vocabularies(rig):
    get_store().promote([promoted_with_body(
        {"device": "Router-A", "event": "link_down", "configured": "link_in_force",
         "text": RAW_CANARY})], now=PROMOTED_AT)
    r = post(EVENTS, headers=events_headers())
    [record] = r.json()["events"]["promoted_conflicts"]
    assert record["text"] == CONFLICT_TEXT and RAW_CANARY not in r.text


def test_a_hypothesis_row_with_a_body_that_does_not_match_its_devices_is_skipped(hyp, caplog):
    bad = new_hypothesis(1, A1, C2, valid_from=VALID_FROM)
    bad = NewPromoted(**{**bad.__dict__, "body": json.dumps({"from": A1, "to": "Router-D/ge0"})})
    promote_hypotheses(bad)
    with caplog.at_level(logging.DEBUG):
        out = ask_topology()
    assert out["graph"]["hypotheses"] == [] and out["graph"]["hypotheses_withheld"] == 0
    assert [r.getMessage() for r in caplog.records if "promoted record" in r.getMessage()] == [
        "promoted record unreadable"]
    assert "Router-D" not in caplog.text
