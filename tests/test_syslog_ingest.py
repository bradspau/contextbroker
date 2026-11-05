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
