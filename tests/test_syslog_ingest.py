"""Syslog ingest: bytes in, events in the state file, quarantined lines and duplicates only counted.

Only requests.post is replaced, by the rdflib-backed stand-in tests.fluent_helpers.GraphFuseki, so
the real device-table query runs. The state file is a real SQLite file (per-test, from conftest).
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from broker_state import store as state_store
from broker_state.store import StateStore
from data_contracts import syslog_events as ev
from ingestion import syslog_ingest as si

T = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
DEVICES = ["Router-A", "Router-B"]

DOWN = (b"<187>1 2026-11-06T11:59:00Z Router-A ifmgr - - - "
        b"%LINK-3-UPDOWN: Interface GigabitEthernet0/0/0, changed state to down")
RFC3164_B = b"<13>Nov  6 11:59:00 router-b sshd[1]: Failed login"
EDGE = b"<13>Nov  6 11:59:00 edge-9 sshd[1]: hello"
MIXED = [DOWN, DOWN, RFC3164_B, EDGE, b"hello", b"\xff\xfe", b"<13>" + b"a" * 5000,
         b"<13>Foo 99 99:99:99 h x"]
ZERO_REASONS = {r: 0 for r in ev.QUARANTINE_REASONS}


def open_store(tmp_path, **kw):
    kw.setdefault("clock", lambda: T)
    return StateStore(str(tmp_path / "broker-state.sqlite3"), **kw)


def window(store, device):
    return store.events_in_window(device, start=datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc),
                                  end=datetime(2026, 11, 6, 13, 0, tzinfo=timezone.utc),
                                  known_at=None, now=T, limit=100)


def test_vocabulary_matches_between_store_and_contract():
    assert state_store.QUARANTINE_REASONS == ev.QUARANTINE_REASONS
    assert state_store.EVENT_FORMATS == ev.EVENT_FORMATS
    assert state_store.EVENT_MAPPINGS == ev.EVENT_MAPPINGS


def test_a_line_is_stored_with_its_device_and_survives_reopen(tmp_path):
    store = open_store(tmp_path)
    summary = si.ingest_lines([DOWN], store=store, devices=DEVICES, clock=lambda: T)
    assert summary == si.IngestSummary(lines=1, stored=1, duplicates=0, unmapped=0,
                                       ambiguous=0, quarantined=ZERO_REASONS)
    store.close()
    again = open_store(tmp_path)
    [row] = window(again, "Router-A")
    assert row.source_host == "Router-A"
    assert row.event_time == datetime(2026, 11, 6, 11, 59, tzinfo=timezone.utc)
    assert row.ingest_time == T
    assert (row.format, row.severity, row.facility) == ("rfc5424", 3, 23)
    assert (row.mapping, row.device, row.kind) == ("mapped", "Router-A", "link_down")
    assert again.event_raw(row.event_id) == DOWN.decode()
    assert row.event_id == ev.event_id(DOWN.decode())
    again.close()


def test_mixed_input_counts_each_outcome(tmp_path):
    store = open_store(tmp_path)
    summary = si.ingest_lines(MIXED, store=store, devices=DEVICES, clock=lambda: T)
    assert (summary.lines, summary.stored, summary.duplicates) == (8, 3, 1)
    assert (summary.unmapped, summary.ambiguous) == (1, 0)
    assert summary.quarantined == {"no priority": 1, "not text": 1, "too long": 1,
                                   "unparsable header": 1}
    counters = store.event_counters()
    assert counters["duplicates"] == 1 and counters["unmapped"] == 1
    assert counters["quarantined"] == summary.quarantined
    assert store.event_count() == 3
    [row] = window(store, "Router-B")
    assert row.source_host == "router-b" and row.event_time is None and row.device == "Router-B"
    for text in ("hello", "Foo 99", "aaaa"):
        assert all(text not in (store.event_raw(r.event_id) or "")
                   for r in window(store, "Router-A") + window(store, "Router-B"))
    store.close()


def test_quarantined_text_is_never_stored(tmp_path):
    import sqlite3
    store = open_store(tmp_path)
    si.ingest_lines([b"canary99x no priority", b"<13>Foo canary99x"], store=store,
                    devices=DEVICES, clock=lambda: T)
    store.close()
    conn = sqlite3.connect(str(tmp_path / "broker-state.sqlite3"))
    dump = "\n".join(conn.iterdump())
    conn.close()
    assert "canary99x" not in dump


def test_ambiguous_host_is_kept_with_both_candidates(tmp_path):
    store = open_store(tmp_path)
    line = b"<13>Nov  6 11:59:00 ROUTER-A sshd[1]: x"
    summary = si.ingest_lines([line], store=store, devices=["Router-A", "router-a"],
                              clock=lambda: T)
    assert (summary.stored, summary.ambiguous, summary.unmapped) == (1, 1, 0)
    for device in ("Router-A", "router-a"):
        [row] = window(store, device)
        assert row.mapping == "ambiguous" and row.device is None
        assert row.candidates == ("Router-A", "router-a")
    store.close()


def test_a_changed_device_table_does_not_remap_a_stored_line(tmp_path):
    store = open_store(tmp_path)
    line = b"<13>Nov  6 11:59:00 router-a sshd[1]: x"
    first = si.ingest_lines([line], store=store, devices=["Router-A"], clock=lambda: T)
    second = si.ingest_lines([line], store=store, devices=["Router-A", "router-a"],
                             clock=lambda: T)
    assert (first.stored, second.stored, second.duplicates) == (1, 0, 1)
    [row] = window(store, "Router-A")
    assert row.mapping == "mapped" and row.device == "Router-A"
    store.close()


def test_batches_are_written_one_transaction_each(tmp_path):
    lines = [b"<13>Nov  6 11:59:0%d Router-A sshd[1]: n%d" % (i, i) for i in range(5)]
    store = open_store(tmp_path)
    calls = []
    real = store.add_events

    def spy(events, *, quarantined=None):
        calls.append(len(list(events)))
        return real(events, quarantined=quarantined)

    store.add_events = spy
    summary = si.ingest_lines(lines, store=store, devices=DEVICES, clock=lambda: T, batch_size=2)
    assert calls == [2, 2, 1]
    assert (summary.lines, summary.stored) == (5, 5)
    assert store.event_count() == 5
    store.close()


def test_a_bad_batch_size_is_a_programming_error(tmp_path):
    store = open_store(tmp_path)
    for bad in (0, -1, 501, True, "2"):
        with pytest.raises(ValueError):
            si.ingest_lines([DOWN], store=store, devices=DEVICES, batch_size=bad)
    store.close()


def test_the_clock_defaults_to_the_aware_utc_now(tmp_path):
    from datetime import timedelta
    store = open_store(tmp_path, clock=lambda: datetime.now(timezone.utc))
    before = datetime.now(timezone.utc)
    si.ingest_lines([b"<13>Nov  6 11:59:00 Router-A sshd[1]: x"], store=store, devices=DEVICES)
    [row] = store.events_in_window("Router-A", start=before - timedelta(minutes=1),
                                   end=before + timedelta(minutes=1), known_at=None,
                                   now=before + timedelta(minutes=1), limit=10)
    assert row.ingest_time.tzinfo is not None
    assert abs((row.ingest_time - before).total_seconds()) < 60
    store.close()


def test_batch_size_constant():
    assert si.BATCH_SIZE == 200


# ---- read_lines (bounded, bytes only) -----------------------------------------------------

import io  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import requests  # noqa: E402
import rdflib  # noqa: E402

from tests.fluent_helpers import NS, GraphFuseki  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEMO = REPO / "ontologies" / "abox" / "topology-demo.ttl"


class NoBulkRead(io.BytesIO):
    def read(self, *a, **k):
        raise AssertionError("the whole stream must never be read at once")


def test_read_lines_splits_strips_one_cr_and_skips_empty_lines():
    assert list(si.read_lines(io.BytesIO(b"a\r\nb\n\nc"))) == [b"a", b"b", b"c"]
    assert list(si.read_lines(io.BytesIO(b"\r\n\n\r\n"))) == []
    assert list(si.read_lines(io.BytesIO(b""))) == []
    assert list(si.read_lines(io.BytesIO(b"x\r\r\n"))) == [b"x\r"]


def test_read_lines_caps_a_huge_line_and_reads_the_next_one():
    data = b"A" * (10 * 1024 * 1024) + b"\nnext\n"
    out = list(si.read_lines(io.BytesIO(data)))
    assert [len(out[0]), out[1]] == [ev.MAX_LINE_BYTES + 1, b"next"]
    assert len(out) == 2


def test_read_lines_keeps_a_line_of_exactly_the_limit():
    exact = b"B" * ev.MAX_LINE_BYTES
    assert list(si.read_lines(io.BytesIO(exact + b"\nz\n"))) == [exact, b"z"]
    assert list(si.read_lines(io.BytesIO(exact))) == [exact]
    over = b"B" * (ev.MAX_LINE_BYTES + 1)
    assert list(si.read_lines(io.BytesIO(over))) == [over]
    assert list(si.read_lines(io.BytesIO(over + b"\nz"))) == [over, b"z"]


def test_read_lines_never_reads_the_whole_stream():
    assert list(si.read_lines(NoBulkRead(b"a\nb\n" + b"c" * 9000 + b"\nd"))) == \
        [b"a", b"b", b"c" * (ev.MAX_LINE_BYTES + 1), b"d"]


# ---- main ---------------------------------------------------------------------------------

MIXED_FILE = b"\n".join(MIXED) + b"\n"
SUMMARY = ("stored=3 duplicates=1 unmapped=1 ambiguous=0 quarantined=4 "
           "(no priority=1, not text=1, too long=1, unparsable header=1)")
QUARANTINE_CANARY = b"<13>Foo canary99x canary99x"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "testpassword0")


def demo_fuseki(monkeypatch, ttl=None):
    g = rdflib.Graph()
    if ttl is None:
        g.parse(DEMO, format="turtle")
    else:
        g.parse(data=ttl, format="turtle")
    fx = GraphFuseki(g)
    monkeypatch.setattr(requests, "post", fx.post)
    return fx


QUERY = ["--query-endpoint", "http://fuseki.test/ds/query"]


def write_input(tmp_path, data=MIXED_FILE):
    p = tmp_path / "in.log"
    p.write_bytes(data)
    return str(p)


def counters(tmp_path):
    store = StateStore(str(tmp_path / "broker-state.sqlite3"))
    try:
        return store.event_count(), store.event_counters()
    finally:
        store.close()


def test_main_prints_one_summary_line_for_a_file(env, monkeypatch, tmp_path, capsys):
    demo_fuseki(monkeypatch)
    assert si.main(["--file", write_input(tmp_path)] + QUERY) == 0
    out = capsys.readouterr()
    assert out.out == SUMMARY + "\n" and out.err == ""
    count, c = counters(tmp_path)
    assert count == 3 and c["duplicates"] == 1


def test_main_reads_standard_input_bytes(env, monkeypatch, tmp_path, capsys):
    demo_fuseki(monkeypatch)

    class FakeStdin:
        buffer = io.BytesIO(MIXED_FILE)

    monkeypatch.setattr(sys, "stdin", FakeStdin())
    assert si.main(QUERY) == 0
    assert capsys.readouterr().out == SUMMARY + "\n"


def test_summary_line_format():
    s = si.IngestSummary(lines=9, stored=1, duplicates=2, unmapped=3, ambiguous=4,
                         quarantined={"no priority": 5, "not text": 0, "too long": 6,
                                      "unparsable header": 7})
    assert si.summary_line(s) == ("stored=1 duplicates=2 unmapped=3 ambiguous=4 quarantined=18 "
                                  "(no priority=5, not text=0, too long=6, unparsable header=7)")


def test_refused_without_the_credential_makes_no_call_and_stores_nothing(monkeypatch, tmp_path,
                                                                         capsys):
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
    fx = demo_fuseki(monkeypatch)
    assert si.main(["--file", write_input(tmp_path)] + QUERY) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.startswith("Refused: ") and "FUSEKI_ADMIN_PASSWORD" in out.err
    assert out.err.endswith(". Nothing was stored.\n") and out.err.count("\n") == 1
    assert fx.posts == []
    count, c = counters(tmp_path)
    assert count == 0 and c["duplicates"] == 0 and c["unmapped"] == 0
    assert set(c["quarantined"].values()) == {0}


def test_refused_when_the_network_graph_cannot_be_reached(env, monkeypatch, tmp_path, capsys):
    def boom(*a, **k):
        raise requests.ConnectionError("canary99x down")

    monkeypatch.setattr(requests, "post", boom)
    assert si.main(["--file", write_input(tmp_path)] + QUERY) == 1
    out = capsys.readouterr()
    assert out.err == "Refused: the network graph could not be reached. Nothing was stored.\n"
    assert out.out == ""
    count, c = counters(tmp_path)
    assert count == 0 and c["duplicates"] == 0 and set(c["quarantined"].values()) == {0}


def test_refused_when_the_device_list_is_unusable(env, monkeypatch, tmp_path, capsys):
    demo_fuseki(monkeypatch, ttl=f'@prefix : <{NS}> . :i a :Interface ; :onDevice "bad canary99x" .')
    assert si.main(["--file", write_input(tmp_path)] + QUERY) == 1
    out = capsys.readouterr()
    assert out.err == ("Refused: the network graph answered with an unusable device list. "
                       "Nothing was stored.\n")
    assert "canary99x" not in out.err + out.out
    count, c = counters(tmp_path)
    assert count == 0 and c["duplicates"] == 0 and set(c["quarantined"].values()) == {0}


def test_the_input_is_not_read_when_the_network_graph_fails(env, monkeypatch, tmp_path):
    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "post", boom)
    opened = []
    real_open = open
    monkeypatch.setattr("builtins.open",
                        lambda p, *a, **k: (opened.append(str(p)), real_open(p, *a, **k))[1])
    src = write_input(tmp_path)
    assert si.main(["--file", src] + QUERY) == 1
    assert src not in opened


def test_refused_with_a_bad_setting_names_the_variable_not_the_value(env, monkeypatch, tmp_path,
                                                                     capsys):
    demo_fuseki(monkeypatch)
    monkeypatch.setenv("BROKER_EVENT_CAP", "canary99x")
    assert si.main(["--file", write_input(tmp_path)] + QUERY) == 1
    err = capsys.readouterr().err
    assert "BROKER_EVENT_CAP" in err and "canary99x" not in err
    assert err.startswith("Refused: ") and err.endswith(". Nothing was stored.\n")


def test_refused_when_the_input_file_cannot_be_read(env, monkeypatch, tmp_path, capsys):
    demo_fuseki(monkeypatch)
    missing = str(tmp_path / "canary99x-missing.log")
    assert si.main(["--file", missing] + QUERY) == 1
    err = capsys.readouterr().err
    assert err == "Refused: the input file could not be read. Nothing was stored.\n"
    assert "canary99x" not in err
    assert si.main(["--file", str(tmp_path)] + QUERY) == 1  # a directory
    assert capsys.readouterr().err == err


def test_refused_when_the_state_store_cannot_open(env, monkeypatch, tmp_path, capsys):
    demo_fuseki(monkeypatch)
    monkeypatch.setenv("BROKER_STATE_PATH", str(tmp_path / "no-such-dir" / "x.sqlite3"))
    assert si.main(["--file", write_input(tmp_path)] + QUERY) == 1
    assert capsys.readouterr().err == "Refused: state store unavailable. Nothing was stored.\n"


def test_refused_after_part_of_the_input_was_stored(env, monkeypatch, tmp_path, capsys):
    from broker_state.store import StateUnavailable
    demo_fuseki(monkeypatch)
    monkeypatch.setattr(si, "BATCH_SIZE", 2)
    real = StateStore.add_events
    calls = []

    def flaky(self, events, *, quarantined=None):
        calls.append(1)
        if len(calls) == 2:
            raise StateUnavailable()
        return real(self, events, quarantined=quarantined)

    monkeypatch.setattr(StateStore, "add_events", flaky)
    lines = [b"<13>Nov  6 11:59:0%d Router-A sshd[1]: n%d" % (i, i) for i in range(6)]
    assert si.main(["--file", write_input(tmp_path, b"\n".join(lines))] + QUERY) == 1
    out = capsys.readouterr()
    assert out.err == "Refused: state store unavailable after 2 events were stored.\n"
    assert out.out == ""
    monkeypatch.setattr(StateStore, "add_events", real)
    assert counters(tmp_path)[0] == 2


def test_an_empty_device_list_warns_and_maps_nothing(env, monkeypatch, tmp_path, capsys):
    demo_fuseki(monkeypatch, ttl="")
    assert si.main(["--file", write_input(tmp_path, DOWN + b"\n")] + QUERY) == 0
    out = capsys.readouterr()
    assert out.out == ("stored=1 duplicates=0 unmapped=1 ambiguous=0 quarantined=0 "
                       "(no priority=0, not text=0, too long=0, unparsable header=0)\n")
    assert out.err == ("Warning: the network graph lists no device, "
                       "so every event is unmapped.\n")


def test_main_waits_for_a_lock_held_on_the_state_file(env, monkeypatch, tmp_path, capsys):
    demo_fuseki(monkeypatch)
    path = str(tmp_path / "broker-state.sqlite3")
    StateStore(path).close()  # create the schema first
    held = threading.Event()

    def hold():
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        held.set()
        time.sleep(0.5)
        conn.execute("ROLLBACK")
        conn.close()

    t = threading.Thread(target=hold)
    t.start()
    held.wait()
    started = time.monotonic()
    code = si.main(["--file", write_input(tmp_path)] + QUERY)
    t.join()
    assert code == 0 and time.monotonic() - started >= 0.3
    assert capsys.readouterr().out == SUMMARY + "\n"


def test_no_output_carries_quarantined_text_or_a_host(env, monkeypatch, tmp_path, capsys):
    demo_fuseki(monkeypatch)
    data = QUARANTINE_CANARY + b"\n<13>Nov  6 11:59:00 canary88y sshd[1]: x\n"
    assert si.main(["--file", write_input(tmp_path, data)] + QUERY) == 0
    out = capsys.readouterr()
    assert "canary99x" not in out.out + out.err and "canary88y" not in out.out + out.err


def test_the_command_has_help_and_no_listener():
    run = subprocess.run([sys.executable, "-m", "ingestion.syslog_ingest", "--help"],
                         capture_output=True, text=True, cwd=REPO,
                         env={**os.environ, "PYTHONPATH": str(REPO)})
    assert run.returncode == 0
    assert "docker exec -i contextbroker-api python -m ingestion.syslog_ingest" in run.stdout
    src = (REPO / "ingestion" / "syslog_ingest.py").read_text()
    assert "socket" not in src and "listen" not in src.replace("Nothing listens", "")
