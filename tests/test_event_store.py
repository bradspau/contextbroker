"""The durable event store: batch write, window read, counters, upgrade of a Phase 4 file.

Events share the Phase 4 state file (D-03). The tables are added at schema version 1
(D-34): the Phase 4 image can still open the file.
"""
import dataclasses
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from broker_state.store import (DEFAULT_EVENT_CAP, DEFAULT_EVENT_RETENTION_S,
                                EVENT_BATCH_MAX, EVENT_FORMATS, EVENT_MAPPINGS,
                                EVICTION_TABLES, QUARANTINE_REASONS, SCHEMA_VERSION,
                                EventBatch, EventRow, NewEvent, StateStore,
                                StateUnavailable)

T = datetime(2026, 5, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)
S = timedelta(seconds=1)
US = timedelta(microseconds=1)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "s.sqlite3"


def eid(n: int) -> str:
    return "evt-" + f"{n:032x}"


def make(n=1, **kw) -> NewEvent:
    base = dict(event_id=eid(n), ingest_time=T, event_time=T - 60 * S, severity=4,
                facility=3, format="rfc5424", source_host="Router-A", mapping="mapped",
                device="Router-A", candidates=("Router-A",), kind="link_down",
                raw=f"<28>1 line number {n}")
    base.update(kw)
    return NewEvent(**base)


def make_unmapped(n=3, **kw) -> NewEvent:
    base = dict(source_host="edge-9", mapping="unmapped", device=None, candidates=(),
                kind=None)
    base.update(kw)
    return make(n, **base)


def make_ambiguous(n=2, **kw) -> NewEvent:
    base = dict(mapping="ambiguous", device=None, candidates=("Router-A", "router-a"),
                kind=None)
    base.update(kw)
    return make(n, **base)


def window(store, device="Router-A", *, start=None, end=None, known_at=None, now=T,
           limit=10):
    return store.events_in_window(device, start=start or T - 900 * S, end=end or T,
                                  known_at=known_at, now=now, limit=limit)


def row_of(e: NewEvent) -> EventRow:
    return EventRow(event_id=e.event_id, ingest_time=e.ingest_time,
                    event_time=e.event_time, severity=e.severity, facility=e.facility,
                    format=e.format, source_host=e.source_host, mapping=e.mapping,
                    device=e.device, candidates=e.candidates, kind=e.kind)


# -- constants -----------------------------------------------------------------

def test_constants_are_the_stated_ones():
    assert DEFAULT_EVENT_CAP == 100000
    assert DEFAULT_EVENT_RETENTION_S == 604800
    assert EVENT_BATCH_MAX == 500
    assert QUARANTINE_REASONS == ("no priority", "not text", "too long",
                                  "unparsable header")
    assert EVENT_FORMATS == ("rfc5424", "rfc3164")
    assert EVENT_MAPPINGS == ("mapped", "ambiguous", "unmapped")


# -- round trip ------------------------------------------------------------------

def test_batch_round_trip_across_reopen(path):
    mapped, amb, unm = make(1), make_ambiguous(2), make_unmapped(3)
    store = StateStore(path)
    batch = store.add_events([mapped, amb, unm])
    assert batch == EventBatch(stored=3, duplicates=0, unmapped=1, ambiguous=1)
    store.close()

    again = StateStore(path)
    rows = window(again)
    assert rows == [row_of(mapped), row_of(amb)] or rows == [row_of(amb), row_of(mapped)]
    by_id = {r.event_id: r for r in rows}
    assert by_id[mapped.event_id] == row_of(mapped)
    assert by_id[mapped.event_id].candidates == ("Router-A",)
    assert by_id[amb.event_id] == row_of(amb)
    assert by_id[amb.event_id].candidates == ("Router-A", "router-a")
    assert [r.event_id for r in window(again, "router-a")] == [amb.event_id]
    assert all(r.mapping != "unmapped" for r in window(again, "edge-9"))
    assert window(again, "edge-9") == []
    for e in (mapped, amb, unm):
        assert again.event_raw(e.event_id) == e.raw
    assert again.event_raw(eid(99)) is None
    assert again.event_count() == 3
    again.close()


def test_newest_placed_first_then_event_id(path):
    store = StateStore(path)
    older = make(1, event_time=T - 120 * S)
    newer = make(2, event_time=T - 30 * S)
    tie_b = make(4, event_time=T - 60 * S)
    tie_a = make(3, event_time=T - 60 * S)
    store.add_events([older, tie_b, newer, tie_a])
    assert [r.event_id for r in window(store)] == [
        newer.event_id, tie_a.event_id, tie_b.event_id, older.event_id]
    store.close()


def test_event_row_has_no_raw_field():
    assert "raw" not in {f.name for f in dataclasses.fields(EventRow)}


def test_event_without_time_is_placed_by_ingest_time(path):
    store = StateStore(path)
    e = make(1, event_time=None, format="rfc3164", ingest_time=T)
    store.add_events([e])
    assert [r.event_id for r in window(store, start=T - 10 * S, end=T)] == [e.event_id]
    only_earlier = window(store, start=T - 900 * S, end=T - 100 * S)
    assert only_earlier == []
    row = window(store)[0]
    assert row.event_time is None
    assert row.format == "rfc3164"
    store.close()


def test_window_ends_are_inclusive(path):
    store = StateStore(path)
    at_start = make(1, event_time=T - 900 * S)
    at_end = make(2, event_time=T)
    after = make(3, event_time=T + US)
    before = make(4, event_time=T - 900 * S - US)
    store.add_events([at_start, at_end, after, before])
    got = {r.event_id for r in window(store)}
    assert got == {at_start.event_id, at_end.event_id}
    store.close()


def test_known_at_cut_off_and_limit(path):
    store = StateStore(path)
    early = make(1, ingest_time=T - 10 * S, event_time=T - 30 * S)
    late = make(2, ingest_time=T, event_time=T - 20 * S)
    store.add_events([early, late])
    got = window(store, known_at=T - 10 * S, now=T)
    assert [r.event_id for r in got] == [early.event_id]
    assert [r.event_id for r in window(store, known_at=T)] == [late.event_id, early.event_id]
    assert [r.event_id for r in window(store, limit=1)] == [late.event_id]
    store.close()


# -- duplicates and counters -----------------------------------------------------

def test_duplicate_is_stored_once_and_counted_across_reopen(path):
    store = StateStore(path)
    first = make(1)
    assert store.add_events([first]) == EventBatch(1, 0, 0, 0)
    later = dataclasses.replace(first, ingest_time=T + 5 * S, mapping="unmapped",
                                device=None, candidates=(), kind=None)
    assert store.add_events([later]) == EventBatch(0, 1, 0, 0)
    row = window(store)[0]
    assert row == row_of(first)
    assert store.event_counters()["duplicates"] == 1
    assert store.event_counters()["unmapped"] == 0
    assert store.event_count() == 1
    store.close()
    again = StateStore(path)
    assert again.event_counters()["duplicates"] == 1
    again.close()


def test_same_id_twice_in_one_call_stores_one_and_counts_one_duplicate(path):
    store = StateStore(path)
    e = make(1)
    assert store.add_events([e, e]) == EventBatch(1, 1, 0, 0)
    assert store.event_count() == 1
    assert store.event_counters()["duplicates"] == 1
    store.close()


def test_quarantine_counts_without_events_and_unmapped_counts_new_rows_only(path):
    store = StateStore(path)
    assert store.add_events([], quarantined={"no priority": 2, "too long": 1}) \
        == EventBatch(0, 0, 0, 0)
    assert store.event_count() == 0
    assert store.event_counters() == {
        "duplicates": 0, "unmapped": 0,
        "quarantined": {"no priority": 2, "not text": 0, "too long": 1,
                        "unparsable header": 0}}
    unm = make_unmapped(3)
    assert store.add_events([unm], quarantined={"no priority": 1}).unmapped == 1
    assert store.add_events([unm]).unmapped == 0  # a duplicate is not counted again
    counters = store.event_counters()
    assert counters["unmapped"] == 1
    assert counters["duplicates"] == 1
    assert counters["quarantined"]["no priority"] == 3
    store.close()
    again = StateStore(path)
    assert again.event_counters() == counters
    again.close()


def test_counters_are_zero_on_a_fresh_store_with_all_four_reasons(path):
    store = StateStore(path)
    c = store.event_counters()
    assert c["duplicates"] == 0 and c["unmapped"] == 0
    assert tuple(c["quarantined"]) == QUARANTINE_REASONS
    assert set(c["quarantined"].values()) == {0}
    store.close()


# -- failure leaves nothing -------------------------------------------------------

class FailingConn:
    """Stands in for the sqlite3 connection (a C type, so it cannot be patched)."""

    def __init__(self, real, prefix, on_call):
        self.real, self.prefix, self.on_call, self.n = real, prefix, on_call, 0

    def execute(self, sql, params=()):
        if sql.startswith(self.prefix):
            self.n += 1
            if self.n == self.on_call:
                raise sqlite3.OperationalError("boom")
        return self.real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.mark.parametrize("prefix,on_call", [("INSERT INTO events", 2),
                                            ("INSERT INTO event_devices", 2),
                                            ("INSERT INTO counters", 1)])
def test_failed_batch_leaves_no_row_and_no_counter(path, prefix, on_call):
    store = StateStore(path)
    real = store._conn
    store._conn = FailingConn(real, prefix, on_call)
    with pytest.raises(StateUnavailable):
        store.add_events([make(1), make(2), make(3), make(3)],
                         quarantined={"not text": 4})
    store._conn = real
    assert store.event_count() == 0
    assert real.execute("SELECT COUNT(*) FROM event_devices").fetchone()[0] == 0
    c = store.event_counters()
    assert c["duplicates"] == 0 and set(c["quarantined"].values()) == {0}
    store.close()


def test_closed_store_is_state_unavailable_for_every_event_method(path):
    store = StateStore(path)
    store.close()
    for call in (lambda: store.add_events([make(1)]),
                 lambda: window(store),
                 lambda: store.event_raw(eid(1)),
                 lambda: store.event_count(),
                 lambda: store.event_counters()):
        with pytest.raises(StateUnavailable):
            call()


# -- programming errors raise ValueError before any SQL ---------------------------

BAD_EVENTS = {
    "id short": lambda: make(1, event_id="evt-abc"),
    "id upper": lambda: make(1, event_id="evt-" + "A" * 32),
    "id trailing newline": lambda: make(1, event_id=eid(1) + "\n"),
    "naive ingest": lambda: make(1, ingest_time=datetime(2026, 5, 1)),
    "naive event time": lambda: make(1, event_time=datetime(2026, 5, 1)),
    "severity 8": lambda: make(1, severity=8),
    "severity true": lambda: make(1, severity=True),
    "severity negative": lambda: make(1, severity=-1),
    "facility 24": lambda: make(1, facility=24),
    "format": lambda: make(1, format="rfc9999"),
    "mapping": lambda: make(1, mapping="maybe"),
    "host empty": lambda: make(1, source_host=""),
    "host 256": lambda: make(1, source_host="a" * 256),
    "host space": lambda: make(1, source_host="a b"),
    "host high": lambda: make(1, source_host="hé"),
    "host del": lambda: make(1, source_host="h\x7f"),
    "mapped no device": lambda: make(1, device=None),
    "mapped other candidates": lambda: make(1, candidates=("Router-A", "Router-B")),
    "mapped empty candidates": lambda: make(1, candidates=()),
    "ambiguous one": lambda: make_ambiguous(2, candidates=("Router-A",)),
    "ambiguous unsorted": lambda: make_ambiguous(2, candidates=("b", "a")),
    "ambiguous repeated": lambda: make_ambiguous(2, candidates=("a", "a")),
    "ambiguous with device": lambda: make_ambiguous(2, device="Router-A"),
    "unmapped with device": lambda: dataclasses.replace(make_unmapped(3), device="Router-A"),
    "unmapped with candidates": lambda: dataclasses.replace(make_unmapped(3),
                                                            candidates=("Router-A",)),
    "device bad chars": lambda: make(1, device="Router A", candidates=("Router A",)),
    "device 129": lambda: make(1, device="d" * 129, candidates=("d" * 129,)),
    "candidate bad chars": lambda: make_ambiguous(2, candidates=("a", "b;c")),
    "kind upper": lambda: make(1, kind="Link"),
    "kind 33": lambda: make(1, kind="a" * 33),
    "kind empty": lambda: make(1, kind=""),
    "raw empty": lambda: make(1, raw=""),
    "raw not str": lambda: make(1, raw=b"x"),
    "raw surrogate": lambda: make(1, raw="a\ud800"),
}


@pytest.mark.parametrize("name", sorted(BAD_EVENTS))
def test_invalid_event_is_value_error_and_changes_nothing(path, name):
    store = StateStore(path)
    good = make(7)
    with pytest.raises(ValueError):
        store.add_events([good, BAD_EVENTS[name]()], quarantined={"not text": 1})
    assert store.event_count() == 0
    assert set(store.event_counters()["quarantined"].values()) == {0}
    store.close()


def test_batch_over_the_maximum_is_value_error(path):
    store = StateStore(path)
    with pytest.raises(ValueError):
        store.add_events([make(i) for i in range(EVENT_BATCH_MAX + 1)])
    assert store.event_count() == 0
    assert store.add_events([make(i) for i in range(EVENT_BATCH_MAX)]).stored == EVENT_BATCH_MAX
    store.close()


@pytest.mark.parametrize("quarantined", [{"nope": 1}, {"no priority": -1},
                                         {"no priority": True}, {"no priority": 1.5}])
def test_bad_quarantine_map_is_value_error(path, quarantined):
    store = StateStore(path)
    with pytest.raises(ValueError):
        store.add_events([], quarantined=quarantined)
    assert set(store.event_counters()["quarantined"].values()) == {0}
    store.close()


@pytest.mark.parametrize("kw", [
    dict(device="a b"), dict(device=""), dict(device="d" * 129),
    dict(start=T, end=T - S),
    dict(start=datetime(2026, 5, 1), end=T),
    dict(end=datetime(2026, 5, 1)),
    dict(now=datetime(2026, 5, 1)),
    dict(known_at=datetime(2026, 5, 1)),
    dict(limit=0), dict(limit=10001), dict(limit=True)])
def test_bad_window_arguments_are_value_error(path, kw):
    store = StateStore(path)
    args = dict(device="Router-A", start=T - 900 * S, end=T, known_at=None, now=T, limit=10)
    args.update(kw)
    device = args.pop("device")
    with pytest.raises(ValueError):
        store.events_in_window(device, **args)
    store.close()


def test_limit_bounds_are_accepted(path):
    store = StateStore(path)
    window(store, limit=1)
    window(store, limit=10000)
    store.close()


# -- hostile text is data ---------------------------------------------------------

def test_hostile_host_and_raw_round_trip_as_data(path):
    store = StateStore(path)
    host = "x';DROP-TABLE-events;--"
    raw = "<13>1 '; DROP TABLE events; -- \"quoted\" \\ %s \x00 end"
    e = make(1, source_host=host, raw=raw)
    amb = make_ambiguous(2, source_host=host, raw=raw + "2")
    store.add_events([e, amb])
    rows = {r.event_id: r for r in window(store)}
    assert rows[e.event_id].source_host == host
    assert store.event_raw(e.event_id) == raw
    assert store.event_raw("evt-0' OR '1'='1") is None
    assert store.event_count() == 2
    store.close()


def test_no_update_statement_on_event_tables():
    import inspect

    import broker_state.store as module

    code = "\n".join(line for line in inspect.getsource(module).splitlines()
                     if not line.lstrip().startswith("#"))
    assert "UPDATE events" not in code
    assert "UPDATE event_devices" not in code


# -- the Phase 4 file upgrades --------------------------------------------------

# Written literally here, not imported from the store: this is what a Phase 4 file holds.
PHASE4_STATEMENTS = (
    "CREATE TABLE outcomes ("
    "intent_id TEXT PRIMARY KEY, "
    "outcome TEXT NOT NULL CHECK (outcome IN ('success','failure','inconclusive')), "
    "written_at_us INTEGER NOT NULL, written_seq INTEGER NOT NULL)",
    "CREATE INDEX outcomes_written_seq ON outcomes (written_seq)",
    "CREATE TABLE marks ("
    "intent_id TEXT NOT NULL, metric_uri TEXT NOT NULL, "
    "observed_at_us INTEGER NOT NULL, value REAL NOT NULL, "
    "written_at_us INTEGER NOT NULL, written_seq INTEGER NOT NULL, "
    "PRIMARY KEY (intent_id, metric_uri))",
    "CREATE INDEX marks_written_seq ON marks (written_seq)",
    "CREATE TABLE situations ("
    "situation_id TEXT PRIMARY KEY, expires_at_us INTEGER NOT NULL, "
    "first_recorded_at_us INTEGER NOT NULL, last_recorded_at_us INTEGER NOT NULL, "
    "repeat_count INTEGER NOT NULL, written_seq INTEGER NOT NULL, body TEXT NOT NULL)",
    "CREATE INDEX situations_expires_at ON situations (expires_at_us)",
    "CREATE INDEX situations_written_seq ON situations (written_seq)",
    "CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)",
)
PHASE4_TABLES = ("outcomes", "marks", "situations", "counters")
SID = "sit-" + "ab" * 16


def phase4_file(path):
    raw = sqlite3.connect(str(path))
    raw.execute("PRAGMA journal_mode=WAL")
    for stmt in PHASE4_STATEMENTS:
        raw.execute(stmt)
    raw.execute("INSERT INTO outcomes VALUES ('i1', 'success', 1, 1)")
    raw.execute("INSERT INTO marks VALUES ('i1', 'urn:m', 5, 2.5, 6, 1)")
    raw.execute("INSERT INTO situations VALUES (?, 10, 3, 4, 2, 1, '{\"a\": 1}')", (SID,))
    raw.execute("INSERT INTO counters VALUES ('evicted_marks', 7)")
    raw.execute("PRAGMA user_version=1")
    raw.commit()
    raw.close()


def schema_sql(path):
    raw = sqlite3.connect(str(path))
    rows = dict(raw.execute(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index')").fetchall())
    version = raw.execute("PRAGMA user_version").fetchone()[0]
    raw.close()
    return rows, version


def test_schema_version_stays_one():
    assert SCHEMA_VERSION == 1


def test_phase4_file_upgrades_without_losing_a_row(path):
    phase4_file(path)
    store = StateStore(path)
    assert store.last_outcome("i1") == "success"
    assert store.marks("i1", ["urn:m"])["urn:m"][1] == 2.5
    assert store.situation_count() == 1
    assert store.evictions()["marks"] == 7
    store.add_events([make(1)])
    assert window(store)[0].event_id == eid(1)
    store.close()

    rows, version = schema_sql(path)
    assert version == 1
    assert "events" in rows and "event_devices" in rows
    phase4 = {s.split()[2]: s for s in PHASE4_STATEMENTS}
    for name, sql in phase4.items():
        assert rows[name] == sql
    raw = sqlite3.connect(str(path))
    assert raw.execute("SELECT intent_id, outcome FROM outcomes").fetchall() == [("i1", "success")]
    assert raw.execute("SELECT * FROM marks").fetchall() == [("i1", "urn:m", 5, 2.5, 6, 1)]
    assert raw.execute("SELECT situation_id, repeat_count, body FROM situations"
                       ).fetchall() == [(SID, 2, '{"a": 1}')]
    assert raw.execute("SELECT value FROM counters WHERE name='evicted_marks'"
                       ).fetchone() == (7,)
    raw.close()


def test_newer_than_one_is_still_refused(path):
    raw = sqlite3.connect(str(path))
    raw.execute("PRAGMA user_version=2")
    raw.close()
    with pytest.raises(StateUnavailable):
        StateStore(path)


def test_reopening_an_upgraded_file_changes_nothing(path):
    phase4_file(path)
    StateStore(path).close()
    before = schema_sql(path)
    store = StateStore(path)
    store.add_events([make(1)])
    store.close()
    StateStore(path).close()
    after = schema_sql(path)
    assert before == after


def test_fresh_file_gets_all_tables_and_the_phase4_statements(path):
    StateStore(path).close()
    rows, version = schema_sql(path)
    assert version == 1
    for name in PHASE4_TABLES + ("events", "event_devices"):
        assert name in rows
    for stmt in PHASE4_STATEMENTS:
        assert rows[stmt.split()[2]] == stmt


def test_fresh_file_creation_is_one_transaction(path, monkeypatch):
    """The event tables are created in the same transaction as the Phase 4 ones: a
    failure part way leaves an empty file at version 0 that opens cleanly later."""
    import broker_state.store as module

    real = module._EVENT_SCHEMA
    monkeypatch.setattr(module, "_EVENT_SCHEMA", real + ("THIS IS NOT SQL",))
    with pytest.raises(StateUnavailable):
        StateStore(path)
    monkeypatch.setattr(module, "_EVENT_SCHEMA", real)
    rows, version = schema_sql(path)
    assert version == 0 and "outcomes" not in rows and "events" not in rows
    StateStore(path).close()
    rows, version = schema_sql(path)
    assert version == 1 and "outcomes" in rows and "events" in rows


def test_events_table_constraints_hold_below_the_validation(path):
    StateStore(path).close()
    raw = sqlite3.connect(str(path))
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO events (event_id, written_seq, ingest_time_us, placed_at_us, "
            "severity, facility, format, source_host, mapping, device, raw) "
            "VALUES ('e', 1, 1, 1, 4, 3, 'rfc5424', 'h', 'mapped', NULL, 'r')")
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO events (event_id, written_seq, ingest_time_us, placed_at_us, "
            "severity, facility, format, source_host, mapping, device, raw) "
            "VALUES ('e', 1, 1, 1, 9, 3, 'rfc5424', 'h', 'unmapped', NULL, 'r')")
    raw.close()


def test_no_log_line_carries_event_text(path, caplog):
    store = StateStore(path)
    with caplog.at_level(logging.DEBUG):
        store.add_events([make(1, source_host="secret-host", raw="secret raw text")])
        window(store)
    assert "secret" not in caplog.text
    store.close()


def test_eviction_tables_keep_phase4_names_before_task3():
    assert EVICTION_TABLES[:3] == ("outcomes", "marks", "situations")
