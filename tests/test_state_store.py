"""The durable state store: round trip across reopen, file mode, failure text."""
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from broker_state.store import SCHEMA_VERSION, StateStore, StateUnavailable

ROOT = Path(__file__).resolve().parent.parent
T = datetime(2026, 5, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)
SID = "sit-" + "0123456789abcdef" * 2


@pytest.fixture
def path(tmp_path):
    return tmp_path / "s.sqlite3"


def test_situation_round_trip_across_reopen(path):
    store = StateStore(path)
    store.record_situation(SID, '{"a": 1}', expires_at=T + timedelta(seconds=300), recorded_at=T)
    store.close()
    again = StateStore(path)
    row = again.get_situation(SID, now=T)
    assert row.body == '{"a": 1}'
    assert row.expires_at == T + timedelta(seconds=300)
    assert row.first_recorded_at == row.last_recorded_at == T
    assert row.repeat_count == 1
    assert again.get_situation("sit-" + "f" * 32, now=T) is None
    assert again.situation_count() == 1
    again.close()


def test_rerecording_replaces_body_and_counts_repeats(path):
    store = StateStore(path)
    store.record_situation(SID, '{"a": 1}', expires_at=T + timedelta(seconds=300), recorded_at=T)
    store.record_situation(SID, '{"a": 2}', expires_at=T + timedelta(seconds=600),
                           recorded_at=T + timedelta(seconds=5))
    row = store.get_situation(SID, now=T)
    assert row.body == '{"a": 2}'
    assert row.expires_at == T + timedelta(seconds=600)
    assert row.first_recorded_at == T
    assert row.last_recorded_at == T + timedelta(seconds=5)
    assert row.repeat_count == 2
    assert store.situation_count() == 1
    store.close()


def test_hostile_ids_are_data_not_sql(path):
    store = StateStore(path)
    nasty = "x'; DROP TABLE situations; --"
    store.record_situation(nasty, "{}", expires_at=T + timedelta(seconds=1), recorded_at=T)
    assert store.get_situation(nasty, now=T).situation_id == nasty
    assert store.situation_count() == 1
    store.close()


def test_file_is_private_and_configured(path):
    store = StateStore(path)
    store.record_situation(SID, "{}", expires_at=T + timedelta(seconds=1), recorded_at=T)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(path) + suffix)
        if f.exists():
            assert f.stat().st_mode & 0o777 == 0o600, suffix
    raw = sqlite3.connect(str(path))
    assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 1
    raw.close()
    assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    store.close()


def test_newer_schema_is_refused(path):
    raw = sqlite3.connect(str(path))
    raw.execute("PRAGMA user_version=2")
    raw.close()
    with pytest.raises(StateUnavailable):
        StateStore(path)


def test_unopenable_path_is_state_unavailable_with_fixed_text(tmp_path, caplog):
    bad = tmp_path / "no-such-dir" / "s.sqlite3"
    with caplog.at_level(logging.WARNING):
        with pytest.raises(StateUnavailable) as info:
            StateStore(bad)
    assert str(info.value) == "state store unavailable"
    assert "state store unavailable: FileNotFoundError" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "no-such-dir" not in caplog.text


def test_state_unavailable_is_not_a_value_error():
    assert not issubclass(StateUnavailable, ValueError)


def test_use_after_close_is_state_unavailable(path):
    store = StateStore(path)
    store.close()
    with pytest.raises(StateUnavailable):
        store.ping()


def test_naive_datetime_is_a_programming_error(path):
    store = StateStore(path)
    with pytest.raises(ValueError):
        store.record_situation(SID, "{}", expires_at=datetime(2026, 5, 1), recorded_at=T)
    with pytest.raises(ValueError):
        store.record_situation(SID, "{}", expires_at=T, recorded_at=datetime(2026, 5, 1))
    assert store.situation_count() == 0
    store.close()


def test_memory_path_works_as_a_constructor_argument(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = StateStore(":memory:")
    store.record_situation(SID, "{}", expires_at=T + timedelta(seconds=1), recorded_at=T)
    assert store.get_situation(SID, now=T).body == "{}"
    store.close()
    assert not (tmp_path / ":memory:").exists()


def test_importing_the_package_opens_no_file(tmp_path):
    target = tmp_path / "import-opens.sqlite3"
    env = {**os.environ, "BROKER_STATE_PATH": str(target), "PYTHONPATH": str(ROOT)}
    subprocess.run([sys.executable, "-c",
                    "import broker_state, broker_state.store, broker_state.process"],
                   check=True, env=env, cwd=str(tmp_path))
    assert not target.exists()
