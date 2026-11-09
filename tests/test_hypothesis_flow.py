"""Stored neighbour-discovery lines to a stored hypothesis to a topology answer (plan 07-07, SEM-01,
D-62 to D-65).

The lines go in through the real ingest, the operator command reads their stored raw text, and the
topology request reads the result back. Fuseki is the rdflib stand-in from tests.test_context_request,
extended with two interfaces no link joins. A hypothesis never counts as evidence: every test that
compares an answer with and without the row asks the same question twice.
"""
import json
import logging
from datetime import timedelta
from unittest.mock import patch

import rdflib
import pytest

from broker_state.process import get_store
from data_contracts.context_contract import ContextResponse
from data_contracts.exchange_profile import from_jsonld, to_jsonld
from data_contracts.promotion import RULE_ADJACENCY, promoted_id
from data_contracts.syslog_events import event_id
from ingestion import promote, syslog_ingest
from tests.fluent_helpers import NS
from tests.test_context_promoted import (  # noqa: F401
    A1, C2, ask_topology, hyp, without_hypotheses)
from tests.test_context_request import (  # noqa: F401
    A_IF, B_IF, EVENTS, NOW, RAW_CANARY, S, _store_clock, client, event, events_headers, post, put,
    rig)
from tests.test_promote_command import RUN_AT, run, situation

DEVICES = ["Router-A", "Router-B", "Router-C"]
AFTER = NOW + 120 * S
F1 = ("%CDP-4-DUPLEX_MISMATCH: duplex mismatch discovered on {local} (not half duplex), "
      "with {peer} {remote} (half duplex).")
F2 = ("%CDP-4-NATIVE_VLAN_MISMATCH: Native VLAN mismatch discovered on {local} (1), "
      "with {peer} {remote} (10).")
F3 = ("LLDP_NEIGHBOR_UP: LLDP neighbor up on local port {local}, remote system {peer}, "
      "remote port {remote}")
LOCAL, PEER, REMOTE = "GigabitEthernet0/0/1", "Router-C", "GigabitEthernet0/0/2"
THREE = "conflicts=0 hypotheses=1 events=3 counted=3 evicted=0"
NONE = "conflicts=0 hypotheses=0 events=3 counted=3 evicted=0"


def line(at, *, template=F1, host="Router-A", local=LOCAL, peer=PEER, remote=REMOTE):
    message = template.format(local=local, peer=peer, remote=remote)
    return f"<188>1 {at:%Y-%m-%dT%H:%M:%SZ} {host} - - - - {message}"


def lines(count=3, **kw):
    """`count` distinct raw lines (the timestamps differ), 100 s apart, the last 100 s before NOW."""
    return [line(NOW - (100 * (count - n)) * S, **kw) for n in range(count)]


def store_lines(raw_lines):
    syslog_ingest.ingest_lines(raw_lines, store=get_store(), devices=DEVICES,
                               clock=lambda: NOW - 59 * S)


def topology_at(when):
    with patch("broker_engine.context_request._utcnow", return_value=when):
        return ask_topology()


def hypothesis_rows():
    return get_store().promoted_rows("hypothesis", "Router-A", valid_at=AFTER, known_at=None,
                                     now=AFTER, limit=10)


def all_rows():
    return get_store()._conn.execute("SELECT * FROM promoted").fetchall()


def promote_lines(raw_lines, capsys, *extra):
    store_lines(raw_lines)
    sid = situation()
    status = run(sid, *extra)
    out = capsys.readouterr()
    return sid, status, out


# ---- the tracer ----

def test_three_stored_neighbour_lines_become_one_hypothesis_a_topology_caller_sees_with_its_support(
        hyp, capsys):
    raw_lines = lines()
    store_lines(raw_lines)
    sid = situation()
    before = topology_at(AFTER)
    triples, seen = len(hyp.fx.graph), len(hyp.queries())
    assert run(sid) == 0
    out = capsys.readouterr()
    assert out.out == THREE + "\n" and out.err == ""
    mine = hyp.queries()[seen:]
    assert mine and all(q.lstrip().startswith(("PREFIX", "SELECT")) for q in mine)
    assert hyp.fx.updates() == [] and len(hyp.fx.graph) == triples
    [row] = hypothesis_rows()
    assert get_store().promoted_count() == 1
    assert (row.kind, row.rule) == ("hypothesis", RULE_ADJACENCY)
    assert (row.device_a, row.device_b) == ("Router-A", "Router-C")
    assert row.promoted_id == promoted_id("hypothesis", RULE_ADJACENCY, (A1, C2))
    assert json.loads(row.body) == {"from": A1, "to": C2}
    assert row.valid_from == NOW - 300 * S and row.promoted_at == RUN_AT
    assert row.situation_id == sid
    assert row.event_ids == tuple(event_id(raw) for raw in raw_lines)
    after = topology_at(AFTER)
    [shown] = after["graph"]["hypotheses"]
    assert (shown["from"], shown["to"]) == (A1, C2)
    assert (shown["label"], shown["state"]) == ("hypothesis", "inferred")
    assert [s["kind"] for s in shown["support"]] == ["situation", "event", "event", "event"]
    assert all(s["state"] == "present" for s in shown["support"])
    assert without_hypotheses(before) == without_hypotheses(after)
    for key in ("nodes", "edges", "limits"):
        assert before["graph"][key] == after["graph"][key]
    assert before["fluent_values"] == after["fluent_values"]
    assert before["eligibility"]["classes"]["topology"] == after["eligibility"]["classes"]["topology"]
    parsed = ContextResponse.model_validate(after)
    assert from_jsonld(to_jsonld(parsed)) == parsed


# ---- every approved format can start a hypothesis ----

@pytest.mark.parametrize("template", [F1, F2, F3])
def test_each_approved_format_makes_the_same_hypothesis(hyp, capsys, template):
    _, status, out = promote_lines(lines(template=template), capsys)
    assert status == 0 and out.out == THREE + "\n"
    [row] = hypothesis_rows()
    assert json.loads(row.body) == {"from": A1, "to": C2}


def test_a_peer_written_in_another_case_is_stored_under_the_graphs_name(hyp, capsys):
    _, status, out = promote_lines(lines(peer="router-c"), capsys)
    assert status == 0 and out.out == THREE + "\n"
    [row] = hypothesis_rows()
    assert json.loads(row.body) == {"from": A1, "to": C2} and row.device_b == "Router-C"
    assert all("router-c" not in str(value) for value in all_rows()[0])


# ---- refusals to infer: each prints hypotheses=0 and writes nothing ----

@pytest.mark.parametrize("kw", [
    {"peer": "Router-Z"},                                  # no such device
    {"remote": "GigabitEthernet0/0/9"},                    # peer interface not in the graph
    {"local": "GigabitEthernet0/0/9"},                     # local interface not in the graph
    {"peer": "Router-A", "remote": LOCAL},                 # an interface with itself
], ids=["unmapped-peer", "unknown-peer-interface", "unknown-local-interface", "own-interface"])
def test_a_name_that_does_not_resolve_stores_no_hypothesis(hyp, capsys, kw):
    _, status, out = promote_lines(lines(**kw), capsys)
    assert status == 0 and out.out == NONE + "\n" and out.err == ""
    assert get_store().promoted_count() == 0


def test_a_peer_that_folds_to_two_devices_stores_no_hypothesis(hyp, capsys):
    hyp.fx.graph.parse(data=f"""@prefix : <{NS}> .
:if-twin a :Interface ; :onDevice "router-c" ; :interfaceName "GigabitEthernet0/0/2" .""",
                       format="turtle")
    store_lines(lines(peer="ROUTER-C"))
    sid = situation()
    assert run(sid) == 0
    assert capsys.readouterr().out == NONE + "\n"
    assert get_store().promoted_count() == 0


def test_lines_naming_the_configured_pair_store_no_hypothesis(hyp, capsys):
    _, status, out = promote_lines(
        lines(local="GigabitEthernet0/0/0", peer="Router-B", remote="GigabitEthernet0/0/1"), capsys)
    assert status == 0 and out.out == NONE + "\n"
    assert get_store().promoted_count() == 0


def test_a_link_in_force_on_one_end_stores_no_hypothesis(hyp, capsys):
    # The unlinked interface Router-A GigabitEthernet0/0/1 gets a link to Router-B in the graph.
    hyp.fx.graph.parse(data=f"""@prefix : <{NS}> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
:if-router-b-ge002 a :Interface ; :onDevice "Router-B" ; :interfaceName "GigabitEthernet0/0/2" .
:link-extra a :Link .
:link-extra-fluent a :LinkFluent ; :ofLink :link-extra ;
  :hasInterface :if-router-a-ge001, :if-router-b-ge002 ;
  :validStart "2026-01-01T00:00:00+00:00"^^xsd:dateTime ;
  :recordedAt "2026-01-01T00:00:00+00:00"^^xsd:dateTime .""", format="turtle")
    _, status, out = promote_lines(lines(), capsys)
    assert status == 0 and out.out == NONE + "\n"
    assert get_store().promoted_count() == 0


def test_two_fluents_in_force_on_one_link_of_a_candidate_interface_store_no_hypothesis(hyp, capsys):
    fluent = rdflib.URIRef(NS + "link-router-a-router-b-fluent-demo")
    again = rdflib.URIRef(NS + "link-router-a-router-b-fluent-twin")
    for p, o in list(hyp.fx.graph.predicate_objects(fluent)):
        hyp.fx.graph.add((again, p, o))
    _, status, out = promote_lines(lines(), capsys)
    assert status == 0 and out.out == NONE + "\n" and out.err == ""
    assert get_store().promoted_count() == 0


def test_two_lines_are_below_the_threshold(hyp, capsys):
    store_lines(lines(2))
    sid = situation()
    assert run(sid) == 0
    assert capsys.readouterr().out == "conflicts=0 hypotheses=0 events=2 counted=2 evicted=0\n"
    assert get_store().promoted_count() == 0


def test_lines_spread_wider_than_the_window_store_no_hypothesis(hyp, capsys, monkeypatch):
    monkeypatch.setenv("BROKER_PROMOTE_WINDOW", "1m")
    _, status, out = promote_lines(lines(), capsys)
    assert status == 0 and out.out == NONE + "\n"
    assert get_store().promoted_count() == 0


# ---- the command's other behaviour ----

def test_a_situation_with_no_neighbour_line_sends_no_device_name_query(hyp, capsys):
    put(event(1, at=NOW - 300 * S), event(2, at=NOW - 200 * S), event(3, at=NOW - 100 * S))
    sid = situation()
    seen = len(hyp.queries())
    assert run(sid) == 0
    capsys.readouterr()
    assert not any("SELECT DISTINCT ?d" in q for q in hyp.queries()[seen:])


def test_a_neighbour_line_reads_the_device_names_then_the_interfaces_then_the_links(hyp, capsys):
    store_lines(lines())
    sid = situation()
    seen = len(hyp.queries())
    assert run(sid) == 0
    capsys.readouterr()
    mine = hyp.queries()[seen:]
    assert any("SELECT DISTINCT ?d" in q for q in mine)
    assert any(":LinkFluent" in q for q in mine)


def test_a_re_run_refreshes_the_hypothesis_and_keeps_its_valid_from(hyp, capsys):
    store_lines(lines())
    sid = situation()
    assert run(sid) == 0
    [first] = hypothesis_rows()
    assert run(sid, at=RUN_AT + 10 * S) == 0
    [second] = hypothesis_rows()
    capsys.readouterr()
    assert (first.repeat_count, second.repeat_count) == (1, 2)
    assert second.valid_from == first.valid_from and second.promoted_id == first.promoted_id
    assert get_store().promoted_count() == 1


def test_a_dry_run_counts_the_hypothesis_and_writes_nothing(hyp, capsys):
    _, status, out = promote_lines(lines(), capsys, "--dry-run")
    assert status == 0 and out.out == "dry run, nothing written: " + THREE + "\n"
    assert get_store().promoted_count() == 0


def test_a_graph_outage_while_reading_the_names_refuses_and_writes_nothing(hyp, capsys):
    import requests
    store_lines(lines())
    sid = situation()
    with patch("requests.post", side_effect=requests.ConnectionError("canary99x down")):
        assert run(sid) == 1
    out = capsys.readouterr()
    assert out.err == f"Refused: {promote.REFUSED_GRAPH_DOWN}. Nothing was promoted.\n"
    assert out.out == "" and get_store().promoted_count() == 0


# ---- nothing a device wrote is stored, printed or logged ----

def test_a_peer_that_maps_to_nothing_and_a_canary_line_leave_no_text_anywhere(hyp, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    evil = lines(peer="Evil-Peer-77")
    canary = [line(NOW - 90 * S, template=F1 + " " + RAW_CANARY)]
    store_lines(evil + canary)
    sid = situation()
    assert run(sid) == 0
    out = capsys.readouterr()
    for text in (out.out, out.err, caplog.text):
        assert "Evil-Peer-77" not in text and RAW_CANARY not in text
    assert "hypotheses=0" in out.out
    assert all("Evil-Peer-77" not in str(v) and RAW_CANARY not in str(v)
               for row in all_rows() for v in row)


def test_a_stored_hypothesis_holds_only_graph_names_ids_and_the_fixed_body_keys(hyp, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    store_lines(lines(peer="router-c"))
    sid = situation()
    assert run(sid) == 0
    out = capsys.readouterr()
    for text in (out.out, out.err, caplog.text):
        assert "router-c" not in text and "GigabitEthernet" not in text
    [row] = hypothesis_rows()
    assert set(json.loads(row.body)) == {"from", "to"}
    assert not any(isinstance(v, str) and ("%CDP" in v or "duplex" in v) for r in all_rows() for v in r)
