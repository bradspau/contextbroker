"""Promoted state through POST /context/request (plan 07-05, SEM-01, D-60 to D-67).

Rows are seeded through the state store here; the operator command (plans 07-04 and 07-07) writes them
for real. A promoted record is read from the state file only and is never evidence: every test that
compares an answer with and without rows asks the same question twice.
"""
import copy

from broker_state.process import get_store
from tests.promotion_helpers import EVENT, SID, new_promoted
from tests.test_context_request import (  # noqa: F401
    EVENTS, NOW, S, _store_clock, client, event, events_headers, olt, post, put, rig)

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
