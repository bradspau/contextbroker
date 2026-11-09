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
