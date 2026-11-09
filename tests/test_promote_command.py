"""The operator command that promotes a stored events situation (plan 07-04, SEM-01).

The route, the token, the state file and the SELECT readers are the real ones; Fuseki is the rdflib
stand-in from tests.test_context_request. A promoted row is written only by this command, so every
test runs it with the clock of the command fixed (`ingestion.promote._utcnow`).
"""
import json
from datetime import timedelta
from unittest.mock import patch

from broker_state.process import get_store
from data_contracts.promotion import RULE_REPEATED_LINK_DOWN, promoted_id
from ingestion import promote
from tests.test_context_request import (  # noqa: F401
    EVENTS, NOW, RAW_CANARY, S, _store_clock, client, event, events_headers, post, put, rig)

QUERY = ["--query-endpoint", "http://fuseki.test/ds/query"]
RUN_AT = NOW + 60 * S
THREE = "conflicts=1 hypotheses=0 events=3 counted=3 evicted=0"


def three_down():
    put(event(1, at=NOW - 300 * S), event(2, at=NOW - 200 * S), event(3, at=NOW - 100 * S))


def situation(body=EVENTS):
    r = post(body, headers=events_headers())
    assert r.status_code == 200, r.text
    return r.json()["situation"]["id"]


def run(sid, *extra, at=RUN_AT):
    with patch("ingestion.promote._utcnow", return_value=at):
        return promote.main(["--situation", sid, *extra, *QUERY])


def rows():
    return get_store().promoted_rows("conflict", "Router-A", valid_at=RUN_AT, known_at=None,
                                     now=RUN_AT, limit=10)


def test_three_link_down_events_on_a_linked_device_store_one_promoted_conflict(rig, capsys):
    three_down()
    sid = situation()
    assert run(sid) == 0
    out = capsys.readouterr()
    assert out.out == THREE + "\n" and out.err == ""
    [row] = rows()
    assert get_store().promoted_count() == 1
    assert (row.kind, row.rule) == ("conflict", RULE_REPEATED_LINK_DOWN)
    assert (row.device_a, row.device_b) == ("Router-A", None)
    assert row.promoted_id == promoted_id("conflict", RULE_REPEATED_LINK_DOWN, ("Router-A",))
    assert row.valid_from == NOW - 300 * S
    assert row.promoted_at == RUN_AT and row.repeat_count == 1
    assert row.situation_id == sid and row.situation_requester == "test-caller"
    assert row.event_ids == tuple("evt-" + f"{n:032x}" for n in (1, 2, 3))
    assert json.loads(row.body) == {
        "configured": "link_in_force", "device": "Router-A", "event": "link_down",
        "text": "event link_down contradicts configured link_in_force"}


def test_the_command_reads_fuseki_with_select_only_and_changes_nothing(rig, capsys):
    three_down()
    sid = situation()
    triples = len(rig.fx.graph)
    seen = len(rig.queries())
    assert run(sid) == 0
    mine = rig.queries()[seen:]
    assert mine and all(q.lstrip().startswith(("PREFIX", "SELECT")) for q in mine)
    assert rig.fx.updates() == [] and len(rig.fx.graph) == triples
    capsys.readouterr()
