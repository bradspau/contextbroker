"""Recording a situation per answered context request, and fetching it back by identity.

The real routes, the real SQLite state store (a per-test file from tests/conftest.py), the rdflib
Fuseki stand-in and the fake VictoriaMetrics from test_context_request. Times are fixed: NOW is the one
clock read of the request handler, and the fetch reads the same patched clock.
"""
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
import requests

from broker_state.process import get_store, reset_store
from broker_state.store import STATE_UNAVAILABLE, StateStore, StateUnavailable
from test_context_request import (  # noqa: F401  (fixtures and helpers)
    NOW, TOPOLOGY, WIDE, assure, client, live_goal, olt, rig, sample, token)
from data_contracts.situation_record import SITUATION_ID_PATTERN
from tests.fluent_helpers import IF0, LAT, T0, bind

NOT_FOUND = {"detail": "situation not found"}
UNAVAILABLE = {"detail": STATE_UNAVAILABLE}
UNKNOWN = "sit-" + "0" * 32


def fetch(situation_id, headers=None, **kw):
    return client.get(f"/context/situation/{situation_id}",
                      headers=token() if headers is None else headers, **kw)


def recorded(rig, headers=None, body=TOPOLOGY):
    r = client.post("/context/request", json=body, headers=token() if headers is None else headers)
    assert r.status_code == 200, r.text
    return r.json()


def at(moment):
    return patch("broker_engine.context_request._utcnow", return_value=moment)


# ---- the tracer: a request is recorded and its requester fetches it back ----

def test_a_request_is_recorded_and_its_requester_fetches_it_by_id(rig):
    answer = recorded(rig)
    store = get_store()
    assert store.situation_count() == 1
    queries_before = len(rig.queries())
    r = fetch(answer["situation"]["id"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["situation"] == answer["situation"]
    assert out["eligibility"] == answer["eligibility"]
    assert out["expires_at"] == answer["expires_at"]
    assert out["expiry_basis"] == answer["expiry_basis"]
    assert out["evidence"]["graph_nodes"] == sorted(n["id"] for n in answer["graph"]["nodes"])
    [link] = answer["fluent_values"]
    assert out["evidence"]["provenance"] == [link["provenance"]]
    assert out["verdict"] is None
    assert out["expired"] is False
    assert out["first_recorded_at"] == NOW.isoformat() and out["last_recorded_at"] == NOW.isoformat()
    assert out["repeat_count"] == 1
    # The fetch reads only the state store: no upstream call, nothing written.
    assert len(rig.queries()) == queries_before
    rig.get.assert_not_called()
    rig.get_intent.assert_not_called()
    assert store.situation_count() == 1


def test_a_recorded_situation_outlives_the_connection(rig):
    first = fetch(recorded(rig)["situation"]["id"])
    reset_store()  # the process store is closed and reopened from the same file
    again = fetch(first.json()["situation"]["id"])
    assert again.status_code == 200 and again.json() == first.json()


def test_a_repeat_replaces_the_record_and_counts_once_more(rig):
    sid = recorded(rig)["situation"]["id"]
    later = NOW + timedelta(milliseconds=500)  # inside the same second, so the same identity
    with at(later):
        assert recorded(rig)["situation"]["id"] == sid
        out = fetch(sid).json()
    assert out["repeat_count"] == 2
    assert out["first_recorded_at"] == NOW.isoformat()
    assert out["last_recorded_at"] == later.isoformat()
    assert get_store().situation_count() == 1


def test_another_requester_an_unknown_id_and_a_malformed_id_get_the_same_404(rig):
    sid = recorded(rig)["situation"]["id"]
    other = fetch(sid, headers=token(sub="someone-else"))
    assert other.status_code == 404 and other.json() == NOT_FOUND
    for bad in (UNKNOWN, "sit-ABC", "sit-" + "g" * 32, "sit-" + "0" * 31, "x", "sit-" + "0" * 33):
        r = fetch(bad)
        assert (r.status_code, r.json()) == (404, NOT_FOUND), bad


def test_a_query_parameter_on_the_fetch_is_422(rig):
    sid = recorded(rig)["situation"]["id"]
    assert fetch(sid, params={"refresh": "1"}).status_code == 422


def test_a_situation_is_expired_from_its_expiry_on(rig):
    answer = recorded(rig)
    assert answer["expiry_basis"] == "configured_state"
    sid = answer["situation"]["id"]
    with at(NOW + timedelta(seconds=299)):
        assert fetch(sid).json()["expired"] is False
    for seconds in (300, 301, 3600):
        with at(NOW + timedelta(seconds=seconds)):
            assert fetch(sid).json()["expired"] is True


def test_the_fetch_reaches_no_upstream_and_never_refreshes_the_record(rig):
    sid = recorded(rig)["situation"]["id"]
    seen = len(rig.queries())
    with at(NOW + timedelta(seconds=20)):
        for _ in range(3):
            assert fetch(sid).status_code == 200
    assert len(rig.queries()) == seen
    rig.get.assert_not_called()
    rig.get_intent.assert_not_called()
    assert get_store().situation_count() == 1
    assert fetch(sid).json()["repeat_count"] == 1


def test_a_state_store_failure_is_a_fixed_503_for_the_request_and_the_fetch(rig):
    with patch.object(StateStore, "record_situation", side_effect=StateUnavailable()):
        r = client.post("/context/request", json=TOPOLOGY, headers=token())
    assert (r.status_code, r.json()) == (503, UNAVAILABLE)
    sid = recorded(rig)["situation"]["id"]
    with patch.object(StateStore, "get_situation", side_effect=StateUnavailable()):
        r = fetch(sid)
    assert (r.status_code, r.json()) == (503, UNAVAILABLE)


def test_the_id_pattern_takes_only_the_id_shape():
    assert SITUATION_ID_PATTERN.match(UNKNOWN)
    assert not SITUATION_ID_PATTERN.match(UNKNOWN + "\n")
    assert not SITUATION_ID_PATTERN.match(UNKNOWN.upper())


# ---- nothing is recorded for a refused or failed request ----

def test_refused_and_failed_requests_record_nothing(rig):
    cases = [
        ("no authority claim", dict(json=TOPOLOGY, headers=token(None)), 403),
        ("entity outside authority", dict(json=TOPOLOGY, headers=token({**WIDE, "entities": ["Router-B"]})), 403),
        ("unknown field", dict(json={**TOPOLOGY, "surprise": 1}, headers=token()), 422),
        ("offset beyond retention", dict(json={**TOPOLOGY, "as_of": "31d"}, headers=token()), 400),
    ]
    for label, kw, status in cases:
        assert client.post("/context/request", **kw).status_code == status, label
        assert get_store().situation_count() == 0, label
    with patch("requests.post", side_effect=requests.ConnectionError("down")):
        r = client.post("/context/request", json=TOPOLOGY, headers=token())
    assert r.status_code == 502
    assert get_store().situation_count() == 0


def test_a_past_request_is_recorded_as_expired_on_the_historical_basis(rig):
    answer = recorded(rig, body={**TOPOLOGY, "as_of": "1h"})
    out = fetch(answer["situation"]["id"]).json()
    assert out["expired"] is True and out["expiry_basis"] == "historical_request"


def test_a_record_past_its_retention_is_gone_before_any_sweep(rig):
    sid = recorded(rig)["situation"]["id"]
    expires = NOW + timedelta(seconds=300)
    with at(expires + timedelta(days=7) - timedelta(seconds=1)):
        assert fetch(sid).status_code == 200
    with at(expires + timedelta(days=7)):
        r = fetch(sid)
    assert (r.status_code, r.json()) == (404, NOT_FOUND)
    assert get_store().situation_count() == 1


# ---- the store keeps references, not data ----

def _keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _keys(item)


def test_the_stored_body_holds_references_only_and_no_raw_value(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt, value=47.25)
    live_goal(olt)
    answer = assure().json()
    assert answer["envelopes"][0]["value"] == 47.25
    row = get_store().get_situation(answer["situation"]["id"], now=NOW)
    assert "47.25" not in row.body
    body = json.loads(row.body)
    assert set(body) == {"situation", "eligibility", "expires_at", "expiry_basis", "evidence", "verdict"}
    assert set(body["evidence"]) == {"provenance", "graph_nodes"}
    assert not {"envelopes", "fluent_values", "value"} & set(_keys(body))


def test_a_request_changes_only_the_situations_table(olt):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    live_goal(olt)
    assert assure().status_code == 200
    store = get_store()
    assert (store.situation_count(), store.outcome_count(), store.mark_count()) == (1, 0, 0)
