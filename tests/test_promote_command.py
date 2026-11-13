"""The operator command that promotes a stored events situation (plan 07-04, SEM-01).

The route, the token, the state file and the SELECT readers are the real ones; Fuseki is the rdflib
stand-in from tests.test_context_request. A promoted row is written only by this command, so every
test runs it with the clock of the command fixed (`ingestion.promote._utcnow`).
"""
import json
import logging
import os
import sqlite3
from dataclasses import replace
from unittest.mock import patch

import pytest
import rdflib
import requests

from broker_state import store as state_store
from broker_state.process import get_store, reset_store
from broker_state.store import StateStore, StateUnavailable
from data_contracts import promotion as vocabulary
from data_contracts.promotion import RULE_REPEATED_LINK_DOWN, promoted_id
from ingestion import promote
from tests import promotion_helpers
from tests.fluent_helpers import NS
from tests.test_context_request import (  # noqa: F401
    EVENTS, NOW, RAW_CANARY, S, _store_clock, ambiguous, client, event, events_headers, post, put,
    rig)

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


# ---- refusals: one fixed line, status 1, nothing written ----

def refused(capsys, text):
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == f"Refused: {text}. Nothing was promoted.\n"
    assert get_store().promoted_count() == 0


def test_refused_without_the_credential(rig, monkeypatch, capsys):
    three_down()
    sid = situation()
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD")
    seen = len(rig.queries())
    assert run(sid) == 1
    out = capsys.readouterr()
    assert out.out == "" and out.err.count("\n") == 1
    assert out.err.startswith("Refused: ") and "FUSEKI_ADMIN_PASSWORD" in out.err
    assert out.err.endswith(". Nothing was promoted.\n")
    assert len(rig.queries()) == seen and get_store().promoted_count() == 0


@pytest.mark.parametrize("value", ["1", "canary99x"])
def test_refused_with_a_bad_threshold_names_the_variable_not_the_value(rig, monkeypatch, capsys,
                                                                       value):
    three_down()
    sid = situation()
    monkeypatch.setenv("BROKER_PROMOTE_MIN_EVENTS", value)
    assert run(sid) == 1
    out = capsys.readouterr()
    assert out.out == "" and out.err.count("\n") == 1
    assert out.err.startswith("Refused: ") and "BROKER_PROMOTE_MIN_EVENTS" in out.err
    assert out.err.endswith(". Nothing was promoted.\n") and "canary99x" not in out.err
    monkeypatch.delenv("BROKER_PROMOTE_MIN_EVENTS")
    assert get_store().promoted_count() == 0


@pytest.mark.parametrize("sid", ["sit-123", "sit-" + "A" * 32, "sit-" + "a" * 32 + "\n", ""])
def test_refused_with_a_malformed_situation_id(rig, capsys, sid):
    assert run(sid) == 1
    refused(capsys, promote.REFUSED_MALFORMED_ID)


def test_refused_with_an_unknown_situation(rig, capsys):
    assert run("sit-" + "0" * 32) == 1
    refused(capsys, promote.REFUSED_NOT_FOUND)


def test_refused_with_a_situation_past_retention(rig, capsys):
    three_down()
    sid = situation()
    assert run(sid, at=NOW + 30 * 24 * 3600 * S) == 1
    refused(capsys, promote.REFUSED_NOT_FOUND)


def test_refused_with_a_situation_that_has_no_events_class(rig, capsys):
    sid = situation({"purpose": "diagnosis", "scope": {"device": "Router-A"}, "classes": ["topology"],
                     "max_depth": 1, "relationship_types": ["has_interface"]})
    seen = len(rig.queries())
    assert run(sid) == 1
    refused(capsys, promote.REFUSED_NO_EVENTS)
    assert len(rig.queries()) == seen


def test_refused_when_the_network_graph_cannot_be_reached(rig, capsys):
    three_down()
    sid = situation()
    with patch("requests.post", side_effect=requests.ConnectionError("canary99x down")):
        assert run(sid) == 1
    refused(capsys, promote.REFUSED_GRAPH_DOWN)


def test_refused_when_the_configured_links_are_unusable(rig, capsys):
    three_down()
    sid = situation()
    fluent = rdflib.URIRef(NS + "link-router-a-router-b-fluent-demo")
    again = rdflib.URIRef(NS + "link-router-a-router-b-fluent-twin")
    for p, o in list(rig.fx.graph.predicate_objects(fluent)):
        rig.fx.graph.add((again, p, o))
    assert run(sid) == 1
    refused(capsys, promote.REFUSED_GRAPH_MALFORMED)


def test_refused_when_the_state_store_cannot_open(rig, capsys):
    three_down()
    sid = situation()
    with patch("ingestion.promote.get_store", side_effect=StateUnavailable()):
        assert run(sid) == 1
    refused(capsys, promote.REFUSED_STATE)


def test_refused_when_the_state_store_fails_in_the_write_writes_nothing(rig, capsys):
    three_down()
    sid = situation()
    with patch.object(StateStore, "promote", side_effect=StateUnavailable()):
        assert run(sid) == 1
    refused(capsys, promote.REFUSED_STATE)


def test_refused_when_the_state_store_fails_in_a_read(rig, capsys):
    three_down()
    sid = situation()
    with patch.object(StateStore, "events_by_ids", side_effect=StateUnavailable()):
        assert run(sid) == 1
    refused(capsys, promote.REFUSED_STATE)


# ---- dry run and re-run ----

def test_dry_run_prints_what_would_be_written_and_writes_nothing(rig, capsys):
    three_down()
    sid = situation()
    with patch.object(StateStore, "promote") as write:
        assert run(sid, "--dry-run") == 0
    write.assert_not_called()
    out = capsys.readouterr()
    assert out.out == "dry run, nothing written: " + THREE + "\n" and out.err == ""
    assert get_store().promoted_count() == 0


def test_a_rerun_refreshes_the_record_instead_of_adding_one(rig, capsys):
    three_down()
    sid = situation()
    assert run(sid) == 0
    later = RUN_AT + 600 * S
    assert run(sid, at=later) == 0
    assert capsys.readouterr().out == THREE + "\n" + THREE + "\n"
    [row] = rows()
    assert get_store().promoted_count() == 1
    assert row.repeat_count == 2 and row.valid_from == NOW - 300 * S
    assert row.promoted_at == RUN_AT and row.last_promoted_at == later


# ---- a device-authored time never counts later than the broker received the line (bead contextbroker-s90) ----

def test_placed_at_is_the_device_time_clamped_to_the_ingest_time():
    from types import SimpleNamespace
    ingest = NOW - 100 * S
    row = lambda event_time: SimpleNamespace(event_time=event_time, ingest_time=ingest)  # noqa: E731
    assert promote._placed_at(row(NOW - 500 * S)) == NOW - 500 * S  # an honest or replayed time stands
    assert promote._placed_at(row(NOW + 900 * S)) == ingest  # a future-dated time is clamped
    assert promote._placed_at(row(ingest)) == ingest
    assert promote._placed_at(row(None)) == ingest


def test_lines_dated_ahead_when_received_do_not_form_a_window_once_their_time_arrives(
        rig, monkeypatch, capsys):
    monkeypatch.setenv("BROKER_PROMOTE_WINDOW", "5m")
    # Three lines received 5000 s, 3000 s and 1000 s ago that all claimed the same time, 100 s ago: dated
    # ahead when they arrived. On the claimed time they sit in one window; on the time they were received
    # (the claim cannot count later than that) they are thousands of seconds apart.
    claimed = NOW - 100 * S
    put(event(1, ingest=NOW - 5000 * S, at=claimed), event(2, ingest=NOW - 3000 * S, at=claimed),
        event(3, ingest=NOW - 1000 * S, at=claimed))
    sid = situation()
    assert run(sid) == 0
    assert capsys.readouterr().out == "conflicts=0 hypotheses=0 events=3 counted=3 evicted=0\n"
    assert get_store().promoted_count() == 0


def test_lines_received_close_together_still_promote_when_dated_slightly_ahead(
        rig, monkeypatch, capsys):
    monkeypatch.setenv("BROKER_PROMOTE_WINDOW", "5m")
    claimed = NOW - 100 * S
    put(event(1, ingest=NOW - 200 * S, at=claimed), event(2, ingest=NOW - 150 * S, at=claimed),
        event(3, ingest=NOW - 120 * S, at=claimed))
    sid = situation()
    assert run(sid) == 0
    assert capsys.readouterr().out == THREE + "\n"
    [row] = rows()
    assert row.valid_from == NOW - 200 * S  # counted at the time received, not the earlier claim


# ---- what is not selected is not stored ----

def test_below_the_threshold_nothing_is_stored_and_fuseki_is_not_asked(rig, capsys):
    put(event(1, at=NOW - 300 * S), event(2, at=NOW - 200 * S))
    sid = situation()
    seen = len(rig.queries())
    assert run(sid) == 0
    assert capsys.readouterr().out == "conflicts=0 hypotheses=0 events=2 counted=2 evicted=0\n"
    assert rig.queries()[seen:] == [] and get_store().promoted_count() == 0


def test_without_a_link_in_force_nothing_is_stored(rig, capsys):
    three_down()
    sid = situation()
    rig.fx.graph.remove((rdflib.URIRef(NS + "link-router-a-router-b-fluent-demo"), None, None))
    assert run(sid) == 0
    assert capsys.readouterr().out == "conflicts=0 hypotheses=0 events=3 counted=3 evicted=0\n"
    assert get_store().promoted_count() == 0


def test_an_ambiguous_event_is_not_counted(rig, capsys):
    put(event(1, at=NOW - 300 * S), event(2, at=NOW - 200 * S), ambiguous(3, at=NOW - 100 * S))
    sid = situation()
    assert run(sid) == 0
    assert capsys.readouterr().out == "conflicts=0 hypotheses=0 events=3 counted=2 evicted=0\n"
    assert get_store().promoted_count() == 0


def test_an_unmapped_or_other_device_row_is_not_counted(rig, capsys):
    three_down()
    sid = situation()
    real = StateStore.events_by_ids

    def with_strangers(self, ids, *, now):
        out = real(self, ids, now=now)
        return out + [replace(out[0], event_id="evt-" + f"{9:032x}", mapping="unmapped", device=None),
                      replace(out[0], event_id="evt-" + f"{10:032x}", device="Router-B")]

    with patch.object(StateStore, "events_by_ids", with_strangers):
        assert run(sid, "--dry-run") == 0
    assert capsys.readouterr().out == (
        "dry run, nothing written: conflicts=1 hypotheses=0 events=5 counted=3 evicted=0\n")


def test_an_event_gone_from_the_store_is_not_counted(rig, capsys):
    three_down()
    sid = situation()
    reset_store()
    conn = sqlite3.connect(os.environ["BROKER_STATE_PATH"])
    conn.execute("DELETE FROM event_devices WHERE event_id = ?", ("evt-" + f"{3:032x}",))
    conn.execute("DELETE FROM events WHERE event_id = ?", ("evt-" + f"{3:032x}",))
    conn.commit()
    conn.close()
    seen = len(rig.queries())
    assert run(sid) == 0
    assert capsys.readouterr().out == "conflicts=0 hypotheses=0 events=2 counted=2 evicted=0\n"
    assert rig.queries()[seen:] == [] and get_store().promoted_count() == 0


# ---- nothing a device or a caller wrote leaves through the command ----

def test_no_raw_line_host_or_requester_reaches_output_logs_or_the_table(rig, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    host = "canary-host-77"
    put(*[event(n, at=NOW - (400 - 100 * n) * S, source_host=host) for n in (1, 2, 3)])
    sid = situation()
    with patch("requests.post", side_effect=requests.ConnectionError("canary99x down")):
        assert run(sid) == 1
    assert run(sid, "--dry-run") == 0
    assert run(sid) == 0
    assert run("sit-123") == 1
    seen = capsys.readouterr()
    shown = seen.out + seen.err + caplog.text
    for secret in (RAW_CANARY, host, "test-caller", "canary99x", "<28>"):
        assert secret not in shown
    reset_store()
    conn = sqlite3.connect(os.environ["BROKER_STATE_PATH"])
    table = str(conn.execute("SELECT * FROM promoted").fetchall())
    conn.close()
    assert "evt-" in table and RAW_CANARY not in table and host not in table


# ---- vocabulary pins and the route ----

def test_the_store_and_the_helpers_use_the_contract_vocabulary():
    assert state_store.PROMOTED_KINDS == vocabulary.PROMOTED_KINDS
    assert state_store.PROMOTED_SUPPORT_MAX == vocabulary.PROMOTED_SUPPORT_MAX
    assert (promotion_helpers.CONFLICT_RULE, promotion_helpers.HYPOTHESIS_RULE) == \
        vocabulary.PROMOTION_RULES


def test_no_route_writes_a_promoted_row(rig):
    three_down()
    with patch.object(StateStore, "promote") as write:
        r = post(EVENTS, headers=events_headers())
        assert r.status_code == 200 and r.json()["events"]["events"]
    write.assert_not_called()
    assert get_store().promoted_count() == 0
