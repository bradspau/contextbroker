"""The JSON-LD exchange profile on POST /context/request (plan 07-06, XCH-01, D-51, D-54, D-66).

The profile is chosen after the request has been answered and recorded, so every test that compares the
two media types asks the same question twice and compares the situation, the stored row and the
upstream calls.
"""
import pytest

from broker_state.process import get_store
from data_contracts.context_contract import ContextResponse
from data_contracts.exchange_profile import (
    CONTEXT, MAX_ACCEPT_ITEMS, MAX_ACCEPT_LENGTH, PROFILE_MEDIA_TYPE, from_jsonld, negotiate)
from tests.test_context_promoted import (  # noqa: F401
    A1, C2, PROMOTED_AT, REQUESTER, VALID_FROM, hyp, hypothesis, promote_conflict, promote_hypotheses)
from tests.test_context_request import (  # noqa: F401
    NOW, TOPOLOGY, _store_clock, client, event, events_headers, post, put)

ASK = {**TOPOLOGY, "classes": ["topology", "events"]}


def ask(headers, accept=None, body=None):
    """POST the body with the given Accept header, or none."""
    sent = dict(headers)
    if accept is not None:
        sent["Accept"] = accept
    return client.post("/context/request", json=body or ASK, headers=sent)


def row(situation_id):
    return get_store().get_situation(situation_id, now=NOW)


# ---- the tracer: the same answer in the other media type, nothing else changed ----

def test_a_caller_asking_for_jsonld_gets_the_profile_of_the_answer_native_json_would_give(hyp):
    put(event(1), event(2), event(3))
    headers = events_headers()
    sid = ask(headers).json()["situation"]["id"]
    promote_conflict(sid)
    promote_hypotheses(hypothesis(2, sid=sid))

    mark = len(hyp.queries())
    native = ask(headers)
    native_queries = hyp.queries()[mark:]
    after_native = row(sid).repeat_count
    mark = len(hyp.queries())
    ld = ask(headers, PROFILE_MEDIA_TYPE)
    ld_queries = hyp.queries()[mark:]

    assert native.status_code == 200 and ld.status_code == 200, (native.text, ld.text)
    assert native.headers["content-type"] == "application/json"
    assert ld.headers["content-type"] == PROFILE_MEDIA_TYPE
    assert native.headers["vary"] == "Accept" and ld.headers["vary"] == "Accept"
    assert ld.json()["@context"] == CONTEXT
    body = native.json()
    assert body["events"]["promoted_conflicts"] and body["graph"]["hypotheses"]
    assert from_jsonld(ld.json()) == ContextResponse.model_validate(body)
    assert ld.json()["situation"]["id"] == body["situation"]["id"] == sid
    assert ld_queries == native_queries and ld_queries
    # one row for the one situation; the profile answer is one more answer to it, nothing new
    assert get_store().situation_count() == 1
    assert row(sid).repeat_count == after_native + 1
    assert hyp.fx.updates() == []


def test_a_request_without_the_header_is_native_json(hyp):
    out = post(TOPOLOGY)
    assert out.status_code == 200
    assert out.headers["content-type"] == "application/json"
    assert out.headers["vary"] == "Accept"
    assert "@context" not in out.json()
