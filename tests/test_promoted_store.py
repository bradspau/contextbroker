"""The promoted-record table in the state file (plan 07-03, SEM-01 storage half).

One additive table at schema version 1 (D-59). Written only by the operator command
of plan 07-04; read by the response in plan 07-05.
"""
import sqlite3
from datetime import datetime, timezone

import pytest

from broker_state.store import SCHEMA_VERSION, EVICTION_TABLES, PromoteBatch, StateStore
from tests.promotion_helpers import new_hypothesis, new_promoted

T = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "s.sqlite3"


def store_at(path, **kw):
    kw.setdefault("clock", lambda: T)
    return StateStore(path, **kw)


def read(store, kind, device, **kw):
    kw.setdefault("valid_at", T)
    kw.setdefault("known_at", None)
    kw.setdefault("now", T)
    kw.setdefault("limit", 10)
    return store.promoted_rows(kind, device, **kw)


def schema_sql(path):
    raw = sqlite3.connect(str(path))
    rows = dict(raw.execute(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index')").fetchall())
    version = raw.execute("PRAGMA user_version").fetchone()[0]
    raw.close()
    return rows, version


# -- Task 1: one record written and read back -----------------------------------

def test_a_promoted_record_survives_a_reopen_and_is_read_for_its_device(path):
    record = new_promoted(1)
    store = store_at(path)
    assert store.promote([record], now=T) == PromoteBatch(written=1, refreshed=0,
                                                          evicted=0, removed=0)
    store.close()

    store = store_at(path)
    rows = read(store, "conflict", "Router-A")
    assert len(rows) == 1
    row = rows[0]
    assert row.promoted_id == record.promoted_id
    assert row.kind == "conflict" and row.rule == record.rule
    assert row.device_a == "Router-A" and row.device_b is None
    assert row.body == record.body
    assert row.valid_from == record.valid_from
    assert row.promoted_at == T and row.last_promoted_at == T
    assert row.repeat_count == 1
    assert row.situation_id == record.situation_id
    assert row.situation_requester == record.situation_requester
    assert row.event_ids == record.event_ids
    assert isinstance(row.event_ids, tuple)
    assert read(store, "conflict", "Router-B") == []
    assert read(store, "hypothesis", "Router-A") == []
    assert store.promoted_count() == 1
    store.close()


def test_the_new_file_has_the_table_and_four_indexes_and_stays_version_one(path):
    store_at(path).close()
    rows, version = schema_sql(path)
    assert version == 1 == SCHEMA_VERSION
    assert "CREATE TABLE IF NOT EXISTS" not in rows["promoted"]  # sqlite stores the bare form
    assert rows["promoted"].startswith("CREATE TABLE promoted")
    for index in ("promoted_written_seq", "promoted_device_a", "promoted_device_b",
                  "promoted_last"):
        assert index in rows
    assert EVICTION_TABLES == ("outcomes", "marks", "situations", "events")


def test_a_hypothesis_is_read_for_both_of_its_devices(path):
    store = store_at(path)
    rec = new_hypothesis(2, "Router-A/GigabitEthernet0/0/1", "Router-C/GigabitEthernet0/0/2")
    store.promote([rec], now=T)
    for device in ("Router-A", "Router-C"):
        rows = read(store, "hypothesis", device)
        assert [r.promoted_id for r in rows] == [rec.promoted_id]
        assert rows[0].device_a == "Router-A" and rows[0].device_b == "Router-C"
    assert read(store, "hypothesis", "Router-B") == []
    assert read(store, "conflict", "Router-A") == []
    store.close()


# -- Task 2: upgrade, upsert, cap, retention, validation, support ---------------

import logging  # noqa: E402
from datetime import timedelta  # noqa: E402

from broker_state.store import (EVENT_BATCH_MAX, PROMOTED_BATCH_MAX,  # noqa: E402
                                PROMOTED_SUPPORT_MAX, SWEEP_INTERVAL_S, _EVENT_SCHEMA,
                                _SCHEMA)
from tests.promotion_helpers import EVENT, SID  # noqa: E402
from tests.test_event_store import eid, make  # noqa: E402

H = timedelta(hours=1)


def head_file(path, *, with_events=True):
    """A file as Phase 4 (_SCHEMA only) or Phase 5 and 6 (_SCHEMA + _EVENT_SCHEMA) made it."""
    raw = sqlite3.connect(str(path))
    raw.execute("PRAGMA journal_mode=WAL")
    for stmt in _SCHEMA + (_EVENT_SCHEMA if with_events else ()):
        raw.execute(stmt)
    raw.execute("INSERT INTO outcomes VALUES ('i1', 'success', 1, 1)")
    raw.execute("INSERT INTO situations VALUES (?, 10, 3, 4, 2, 1, '{\"a\": 1}')", (SID,))
    if with_events:
        raw.execute(
            "INSERT INTO events VALUES (?, 1, 5, NULL, 5, 4, 3, 'rfc5424', 'Router-A', "
            "'mapped', 'Router-A', 'link_down', 'raw line')", (eid(1),))
        raw.execute("INSERT INTO event_devices VALUES (?, 'Router-A')", (eid(1),))
    raw.execute("PRAGMA user_version=1")
    raw.commit()
    raw.close()


def test_a_phase5_or_6_file_gains_the_table_and_keeps_every_row(path):
    head_file(path)
    before, _ = schema_sql(path)
    store = store_at(path)
    store.promote([new_promoted(1)], now=T)
    store.close()
    after, version = schema_sql(path)
    assert version == 1
    assert "promoted" in after
    for name, sql in before.items():
        assert after[name] == sql  # every earlier table and index is byte-identical
    raw = sqlite3.connect(str(path))
    assert raw.execute("SELECT intent_id FROM outcomes").fetchall() == [("i1",)]
    assert raw.execute("SELECT situation_id, repeat_count FROM situations"
                       ).fetchall() == [(SID, 2)]
    assert raw.execute("SELECT event_id FROM events").fetchall() == [(eid(1),)]
    raw.close()
    # a second open changes no schema SQL
    mid, _ = schema_sql(path)
    store_at(path).close()
    assert schema_sql(path)[0] == mid


def test_a_phase4_file_gains_both_the_event_tables_and_the_promoted_table(path):
    head_file(path, with_events=False)
    store_at(path).close()
    rows, version = schema_sql(path)
    assert version == 1
    for name in ("events", "event_devices", "promoted", "promoted_last"):
        assert name in rows


def test_a_re_run_keeps_the_first_record_and_only_counts(path):
    # Bead contextbroker-5re: the first promotion's valid_from, support, situation and requester stay, so a
    # read as known at an earlier time never shows support that did not exist then.
    store = store_at(path)
    early = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
    late = datetime(2026, 11, 6, 11, 0, tzinfo=timezone.utc)
    first = new_promoted(1, valid_from=late, event_ids=(EVENT(1),))
    store.promote([first], now=T)
    sid2 = "sit-" + "b" * 32
    again = new_promoted(1, valid_from=early, situation_id=sid2, requester="bob",
                         event_ids=(EVENT(7), EVENT(8)))
    batch = store.promote([again], now=T + H)
    assert (batch.written, batch.refreshed) == (0, 1)
    row = read(store, "conflict", "Router-A", now=T + H, valid_at=T + H)[0]
    assert row.valid_from == late
    assert row.situation_id == SID and row.situation_requester == "alice"
    assert row.event_ids == (EVENT(1),)
    assert row.promoted_at == T and row.last_promoted_at == T + H
    assert row.repeat_count == 2
    store.promote([new_promoted(1, valid_from=T)], now=T + 2 * H)
    row = read(store, "conflict", "Router-A", now=T + 2 * H, valid_at=T + 2 * H)[0]
    assert row.valid_from == late and row.repeat_count == 3 and row.last_promoted_at == T + 2 * H
    assert store.promoted_count() == 1
    store.close()


def test_a_read_as_known_before_a_re_run_sees_what_was_known_then(path):
    store = store_at(path)
    store.promote([new_promoted(1, event_ids=(EVENT(1),))], now=T)
    store.promote([new_promoted(1, situation_id="sit-" + "c" * 32, requester="mallory",
                                event_ids=(EVENT(9),))], now=T + H)
    [then] = read(store, "conflict", "Router-A", now=T + 2 * H, valid_at=T + 2 * H, known_at=T + timedelta(seconds=1))
    assert then.event_ids == (EVENT(1),) and then.situation_requester == "alice"
    [now_] = read(store, "conflict", "Router-A", now=T + 2 * H, valid_at=T + 2 * H)
    assert now_.event_ids == (EVENT(1),) and now_.situation_requester == "alice"
    store.close()


def test_a_record_past_retention_that_is_promoted_again_starts_over_with_the_new_support(path):
    store = store_at(path)
    store.promote([new_promoted(1, event_ids=(EVENT(1),))], now=T)
    far = T + timedelta(seconds=store.promoted_retention_s + 1)
    store.promote([new_promoted(1, requester="bob", event_ids=(EVENT(5),))], now=far)
    [row] = read(store, "conflict", "Router-A", now=far, valid_at=far)
    assert row.repeat_count == 1 and row.event_ids == (EVENT(5),) and row.situation_requester == "bob"
    store.close()


def test_the_cap_evicts_the_least_recently_written_with_a_count_and_one_fixed_line(
        path, caplog):
    store = store_at(path, promoted_cap=3)
    with caplog.at_level(logging.WARNING, logger="broker_state.store"):
        batch = store.promote([new_promoted(n, device=f"Router-{n}") for n in range(1, 6)],
                              now=T)
    assert batch.evicted == 2 and batch.written == 5
    assert store.promoted_count() == 3
    assert store.promoted_evictions() == 2
    kept = [n for n in range(1, 6) if read(store, "conflict", f"Router-{n}")]
    assert kept == [3, 4, 5]
    lines = [r.getMessage() for r in caplog.records if "table=promoted" in r.getMessage()]
    assert lines == ["state eviction: table=promoted evicted=2"]
    text = " ".join(lines)
    for secret in ("prm-", "Router", "alice", "evt-"):
        assert secret not in text
    assert set(store.evictions()) == set(EVICTION_TABLES)
    store.close()


def test_an_expired_row_is_hidden_at_once_and_swept_once_a_minute_with_a_fixed_line(
        path, caplog):
    ticks = [0.0]
    store = store_at(path, promoted_retention_s=3600, monotonic=lambda: ticks[0])
    store.promote([new_promoted(1)], now=T)  # the first call also runs the first sweep
    assert len(read(store, "conflict", "Router-A", now=T + H - timedelta(seconds=1))) == 1
    assert read(store, "conflict", "Router-A", now=T + H) == []
    assert store.promoted_count() == 1  # hidden, not yet swept

    ticks[0] = SWEEP_INTERVAL_S - 1  # inside the minute: no sweep
    with caplog.at_level(logging.WARNING, logger="broker_state.store"):
        batch = store.promote([new_promoted(2, device="Router-B")],
                              now=T + H + timedelta(seconds=61))
    assert batch.removed == 0 and store.promoted_count() == 2

    ticks[0] = SWEEP_INTERVAL_S + 1
    with caplog.at_level(logging.WARNING, logger="broker_state.store"):
        batch = store.promote([new_promoted(3, device="Router-C")],
                              now=T + H + timedelta(seconds=62))
    assert batch.removed == 1
    assert store.promoted_count() == 2
    lines = [r.getMessage() for r in caplog.records if "table=promoted" in r.getMessage()]
    assert lines == ["state retention: table=promoted removed=1"]
    store.close()


def test_a_row_past_retention_that_is_promoted_again_starts_over(path):
    store = store_at(path, promoted_retention_s=3600)
    store.promote([new_promoted(1)], now=T)
    batch = store.promote([new_promoted(1)], now=T + 2 * H)
    assert (batch.written, batch.refreshed) == (1, 0)
    row = read(store, "conflict", "Router-A", now=T + 2 * H, valid_at=T + 2 * H)[0]
    assert row.repeat_count == 1 and row.promoted_at == T + 2 * H
    store.close()


def test_reads_filter_by_valid_at_known_at_newest_first_and_limit(path):
    store = store_at(path)
    store.promote([new_promoted(1, valid_from=T - 3 * H)], now=T)
    store.promote([new_promoted(2, valid_from=T - 2 * H), new_promoted(3, valid_from=T - H)],
                  now=T + H)
    now = T + H
    ids = [r.promoted_id for r in read(store, "conflict", "Router-A", now=now, valid_at=now)]
    assert ids == [new_promoted(3).promoted_id, new_promoted(2).promoted_id,
                   new_promoted(1).promoted_id]
    cut = read(store, "conflict", "Router-A", now=now, valid_at=T - 2 * H)
    assert [r.promoted_id for r in cut] == [new_promoted(2).promoted_id,
                                            new_promoted(1).promoted_id]
    known = read(store, "conflict", "Router-A", now=now, valid_at=now, known_at=T)
    assert [r.promoted_id for r in known] == [new_promoted(1).promoted_id]
    assert len(read(store, "conflict", "Router-A", now=now, valid_at=now, limit=2)) == 2
    store.close()


@pytest.mark.parametrize("change", [
    {"promoted_id": "prm-xyz"},
    {"promoted_id": "prm-" + "A" * 32},
    {"kind": "fact"},
    {"rule": "Bad Rule"},
    {"rule": ""},
    {"device_a": "bad device"},
    {"device_b": "Router-B"},                       # a conflict has no second device
    {"body": "not json"},
    {"body": "[1, 2]"},
    {"body": '{"a": NaN}'},
    {"body": '{"a": "' + "x" * 4096 + '"}'},
    {"valid_from": datetime(2026, 11, 6, 11, 0)},   # naive
    {"situation_id": "sit-short"},
    {"situation_requester": ""},
    {"situation_requester": "al\nice"},
    {"situation_requester": "a" * 1025},
    {"event_ids": ()},
    {"event_ids": [EVENT(1)]},                      # a list, not a tuple
    {"event_ids": (EVENT(1), EVENT(1))},
    {"event_ids": ("evt-nope",)},
    {"event_ids": tuple(EVENT(n) for n in range(PROMOTED_SUPPORT_MAX + 1))},
])
def test_every_refusal_is_a_value_error_before_any_sql(path, change):
    import dataclasses
    store = store_at(path)
    bad = dataclasses.replace(new_promoted(1), **change)
    with pytest.raises(ValueError):
        store.promote([new_promoted(9), bad], now=T)
    assert store.promoted_count() == 0
    store.close()


def test_a_hypothesis_needs_a_second_device_and_a_batch_has_limits(path):
    import dataclasses
    store = store_at(path)
    hyp = new_hypothesis(2, "Router-A/Gi0", "Router-C/Gi1")
    with pytest.raises(ValueError):
        store.promote([dataclasses.replace(hyp, device_b=None)], now=T)
    with pytest.raises(ValueError):
        store.promote([new_promoted(1), new_promoted(1)], now=T)  # one id twice
    with pytest.raises(ValueError):
        store.promote([new_promoted(n) for n in range(1, PROMOTED_BATCH_MAX + 2)], now=T)
    with pytest.raises(ValueError):
        store.promote(["not a record"], now=T)
    with pytest.raises(ValueError):
        read(store, "fact", "Router-A")
    with pytest.raises(ValueError):
        read(store, "conflict", "bad device")
    with pytest.raises(ValueError):
        read(store, "conflict", "Router-A", limit=0)
    assert store.promoted_count() == 0
    store.close()


def test_a_rejected_write_logs_nothing(path, caplog):
    store = store_at(path, promoted_cap=1)
    with caplog.at_level(logging.WARNING, logger="broker_state.store"):
        with pytest.raises(ValueError):
            store.promote([new_promoted(1), new_promoted(2),
                           new_promoted(3, device="bad device")], now=T)
    assert [r for r in caplog.records if "promoted" in r.getMessage()] == []
    store.close()


def test_a_record_with_injection_text_in_its_fields_is_stored_as_data(path):
    store = store_at(path)
    store.promote([new_promoted(1, requester="x'); DROP TABLE promoted;--")], now=T)
    row = read(store, "conflict", "Router-A")[0]
    assert row.situation_requester == "x'); DROP TABLE promoted;--"
    assert store.promoted_count() == 1
    store.close()


def test_support_is_reported_present_or_evicted_by_the_reads_own_cut_offs(path):
    store = store_at(path)
    store.record_situation(SID, "{}", expires_at=T, recorded_at=T)
    store.add_events([make(1, ingest_time=T, event_time=None)])
    present = store.support_present(SID, (eid(1), eid(2)), now=T)
    assert present == (True, (True, False))
    # past both retentions (7 d each), with no sweep having run
    late = T + timedelta(days=7, seconds=1)
    assert store.support_present(SID, (eid(1), eid(2)), now=late) == (False, (False, False))
    assert store.support_present("sit-" + "c" * 32, (), now=T) == (False, ())
    with pytest.raises(ValueError):
        store.support_present("bad", (eid(1),), now=T)
    with pytest.raises(ValueError):
        store.support_present(SID, ("evt-bad",), now=T)
    store.close()


def test_events_by_ids_keeps_the_given_order_and_skips_absent_and_expired(path):
    store = store_at(path)
    store.add_events([make(1, ingest_time=T, event_time=None),
                      make(3, ingest_time=T, event_time=None),
                      make(5, ingest_time=T - timedelta(days=8), event_time=None)])
    rows = store.events_by_ids([eid(3), eid(1), eid(2), eid(5)], now=T)
    assert [r.event_id for r in rows] == [eid(3), eid(1)]
    assert not hasattr(rows[0], "raw")
    assert rows[0].device == "Router-A" and rows[0].candidates == ("Router-A",)
    with pytest.raises(ValueError):
        store.events_by_ids(["evt-bad"], now=T)
    with pytest.raises(ValueError):
        store.events_by_ids([eid(n) for n in range(EVENT_BATCH_MAX + 1)], now=T)
    store.close()


def test_a_closed_store_leaves_every_new_method_as_state_unavailable(path):
    from broker_state.store import StateUnavailable
    store = store_at(path)
    store.close()
    for call in (lambda: store.promote([new_promoted(1)], now=T),
                 lambda: read(store, "conflict", "Router-A"),
                 store.promoted_count, store.promoted_evictions,
                 lambda: store.support_present(SID, (eid(1),), now=T),
                 lambda: store.events_by_ids([eid(1)], now=T)):
        with pytest.raises(StateUnavailable):
            call()


def test_promoted_stays_out_of_the_pinned_eviction_tables():
    import inspect

    import broker_state.store as module

    assert EVICTION_TABLES == ("outcomes", "marks", "situations", "events")
    code = "\n".join(line for line in inspect.getsource(module).splitlines()
                     if not line.lstrip().startswith("#"))
    for table in (module._COUNT_SQL, module._EVICT_SQL, module._COUNTER_NAME):
        assert "promoted" not in table
    assert "UPDATE promoted" not in code or "ON CONFLICT(promoted_id)" in code
