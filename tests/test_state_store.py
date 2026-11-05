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


# -- outcomes and marks (Task 2) -----------------------------------------

U = "http://example.org/metric#latency"
V = "http://example.org/metric#jitter"


def test_outcome_round_trip_replace_and_reopen(path):
    store = StateStore(path)
    store.record_outcome("i1", "failure")
    assert store.last_outcome("i1") == "failure"
    store.record_outcome("i1", "success")
    assert store.outcome_count() == 1
    assert store.last_outcome("nope") is None
    store.close()
    again = StateStore(path)
    assert again.last_outcome("i1") == "success"
    again.close()


def test_outcome_outside_the_three_values_is_refused_before_sql(path):
    store = StateStore(path)
    with pytest.raises(ValueError):
        store.record_outcome("i1", "maybe")
    assert store.outcome_count() == 0
    store.close()


def test_outcome_check_constraint_is_the_backstop(path):
    store = StateStore(path)
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute("INSERT INTO outcomes VALUES ('i', 'maybe', 0, 0)")
    store.close()


def test_mark_compare_semantics(path):
    store = StateStore(path)
    assert store.advance_mark("i1", U, T, 42.0) is True
    assert store.marks("i1", [U, V]) == {U: (T, 42.0)}
    assert store.advance_mark("i1", U, T - timedelta(microseconds=1), 1.0) is False
    assert store.advance_mark("i1", U, T, 43.0) is False
    assert store.advance_mark("i1", U, T, 42.0) is True
    assert store.advance_mark("i1", U, T + timedelta(seconds=1), 1.0) is True
    assert store.marks("i1", [U]) == {U: (T + timedelta(seconds=1), 1.0)}
    assert store.mark_count() == 1
    store.close()


def test_mark_time_round_trips_to_the_microsecond(path):
    store = StateStore(path)
    t = datetime(2026, 5, 1, 12, 0, 0, 999999, tzinfo=timezone.utc)
    store.advance_mark("i1", U, t, 1.5)
    assert store.marks("i1", [U])[U][0] == t
    store.close()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "1", None])
def test_mark_value_must_be_a_finite_number(path, bad):
    store = StateStore(path)
    store.advance_mark("i1", U, T, 1.0)
    with pytest.raises(ValueError):
        store.advance_mark("i1", U, T + timedelta(seconds=1), bad)
    assert store.marks("i1", [U]) == {U: (T, 1.0)}
    store.close()


def test_hostile_intent_and_metric_are_data(path):
    store = StateStore(path)
    nasty = "i'; DELETE FROM marks; --"
    assert store.advance_mark(nasty, U, T, 1.0) is True
    store.record_outcome(nasty, "success")
    assert store.marks(nasty, [U]) == {U: (T, 1.0)}
    assert store.last_outcome(nasty) == "success"
    store.close()


def test_restart_keeps_marks_and_outcomes(path):
    store = StateStore(path)
    store.advance_mark("i1", U, T, 7.0)
    store.record_outcome("i1", "inconclusive")
    store.close()
    again = StateStore(path)
    assert again.marks("i1", [U]) == {U: (T, 7.0)}
    assert again.last_outcome("i1") == "inconclusive"
    again.close()


_SUBPROC = (
    "import sys\n"
    "from datetime import datetime, timedelta, timezone\n"
    "from broker_state.store import StateStore\n"
    "t = datetime.fromisoformat(sys.argv[2])\n"
    "s = StateStore(sys.argv[1])\n"
    "print(s.advance_mark('i1', sys.argv[3], t - timedelta(seconds=10), 0.5))\n"
    "print(s.marks('i1', [sys.argv[3]])[sys.argv[3]][1])\n"
)


def _run_sub(path, uri):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-c", _SUBPROC, str(path), T.isoformat(), uri],
                          env=env, capture_output=True, text=True, timeout=30)


def test_second_process_sees_the_mark_and_cannot_move_it_back(path):
    store = StateStore(path)
    store.advance_mark("i1", U, T, 42.0)
    done = _run_sub(path, U)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["False", "42.0"]
    store.close()


def test_second_process_waits_for_a_held_write_lock(path):
    import threading
    import time
    store = StateStore(path)
    store.advance_mark("i1", U, T, 42.0)
    holder = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    threading.Timer(0.5, lambda: holder.execute("ROLLBACK")).start()
    started = time.monotonic()
    done = _run_sub(path, U)
    assert done.returncode == 0, done.stderr
    assert time.monotonic() - started >= 0.4
    assert done.stdout.split() == ["False", "42.0"]
    assert store.marks("i1", [U])[U] == (T, 42.0)
    holder.close()
    store.close()


def test_outcome_eviction_drops_least_recently_written(path, caplog):
    store = StateStore(path, outcome_cap=2)
    store.record_outcome("a", "success")
    store.record_outcome("b", "failure")
    store.record_outcome("a", "failure")  # a is now the most recently written
    assert store.evictions()["outcomes"] == 0  # re-writing evicts nothing
    with caplog.at_level(logging.WARNING):
        store.record_outcome("c", "success")
    assert store.last_outcome("b") is None
    assert store.last_outcome("a") == "failure"
    assert store.outcome_count() == 2
    assert store.evictions()["outcomes"] == 1
    lines = [r.getMessage() for r in caplog.records if "eviction" in r.getMessage()]
    assert lines == ["state eviction: table=outcomes evicted=1"]
    store.close()
    again = StateStore(path, outcome_cap=2)
    assert again.evictions()["outcomes"] == 1
    again.close()


def test_mark_eviction_is_keyed_by_intent_and_metric(path, caplog):
    store = StateStore(path, mark_cap=2)
    store.advance_mark("i1", U, T, 1.0)
    store.advance_mark("i1", V, T, 1.0)
    store.advance_mark("i1", U, T + timedelta(seconds=1), 2.0)
    with caplog.at_level(logging.WARNING):
        store.advance_mark("i2", U, T, 3.0)
    assert store.marks("i1", [U, V]).keys() == {U}
    assert store.mark_count() == 2
    assert store.evictions()["marks"] == 1
    assert [r.getMessage() for r in caplog.records] == ["state eviction: table=marks evicted=1"]
    store.close()


def test_situation_eviction(path, caplog):
    store = StateStore(path, situation_cap=2)
    exp = T + timedelta(days=1)
    store.record_situation("s1", "{}", expires_at=exp, recorded_at=T)
    store.record_situation("s2", "{}", expires_at=exp, recorded_at=T)
    store.record_situation("s1", "{}", expires_at=exp, recorded_at=T)
    with caplog.at_level(logging.WARNING):
        store.record_situation("s3", "{}", expires_at=exp, recorded_at=T)
    assert store.get_situation("s2", now=T) is None
    assert store.get_situation("s1", now=T).repeat_count == 2
    assert store.situation_count() == 2
    assert store.evictions() == {"outcomes": 0, "marks": 0, "situations": 1}
    assert [r.getMessage() for r in caplog.records] == [
        "state eviction: table=situations evicted=1"]
    store.close()


def test_a_failed_write_rolls_back_and_logs_no_eviction(path, caplog):
    calls = []

    def clock():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("boom")
        return T

    store = StateStore(path, outcome_cap=1, clock=clock)
    store.record_outcome("a", "success")
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError):
            store.record_outcome("b", "success")
    assert store.last_outcome("a") == "success"
    assert store.outcome_count() == 1
    assert store.evictions()["outcomes"] == 0
    assert "eviction" not in caplog.text
    store.close()


# -- retention (Task 3) ---------------------------------------------------

HOUR = timedelta(hours=1)


def test_retention_read_side_cut_off_without_a_sweep(path):
    store = StateStore(path, situation_retention_s=7 * 86400)
    expires = T + timedelta(minutes=5)
    store.record_situation(SID, "{}", expires_at=expires, recorded_at=T)
    edge = expires + timedelta(days=7)
    assert store.get_situation(SID, now=edge - timedelta(microseconds=1)) is not None
    assert store.get_situation(SID, now=edge) is None
    assert store.situation_count() == 1  # still physically present, never swept
    store.close()


def test_retention_sweep_inside_a_write_removes_only_old_situations(path, caplog):
    mono = [0.0]
    store = StateStore(path, situation_retention_s=3600, monotonic=lambda: mono[0])
    store.record_situation("old", "{}", expires_at=T - 2 * HOUR, recorded_at=T - 3 * HOUR)
    store.record_situation("edge", "{}", expires_at=T - HOUR, recorded_at=T - 3 * HOUR)
    store.record_situation("young", "{}", expires_at=T - timedelta(minutes=30),
                           recorded_at=T - 3 * HOUR)
    store.record_outcome("i1", "success")
    store.advance_mark("i1", U, T - 5 * HOUR, 1.0)
    assert store.situation_count() == 3
    mono[0] = 100.0
    with caplog.at_level(logging.WARNING):
        store.record_situation("new", "{}", expires_at=T + HOUR, recorded_at=T)
    assert store.get_situation("old", now=T - 3 * HOUR) is None
    assert store.situation_count() == 2  # old and edge removed; young and new stay
    assert store.last_outcome("i1") == "success"
    assert store.mark_count() == 1
    lines = [r.getMessage() for r in caplog.records if "retention" in r.getMessage()]
    assert lines == ["state retention: table=situations removed=2"]
    store.close()


def test_retention_sweep_logs_nothing_when_nothing_is_removed(path, caplog):
    store = StateStore(path, situation_retention_s=3600)
    with caplog.at_level(logging.WARNING):
        store.record_situation(SID, "{}", expires_at=T + HOUR, recorded_at=T)
    assert "retention" not in caplog.text
    store.close()


def test_retention_sweep_is_paced_by_the_monotonic_clock(path):
    mono = [0.0]
    store = StateStore(path, situation_retention_s=3600, monotonic=lambda: mono[0])
    store.record_situation("a", "{}", expires_at=T + HOUR, recorded_at=T)  # first write sweeps
    store.record_situation("old", "{}", expires_at=T - 2 * HOUR, recorded_at=T - 3 * HOUR)
    mono[0] = 59.9
    store.record_situation("b", "{}", expires_at=T + HOUR, recorded_at=T)
    assert store.situation_count() == 3  # no sweep yet: "old" is still there
    assert store.get_situation("old", now=T) is None  # but hidden from reads
    mono[0] = 60.0
    store.record_situation("c", "{}", expires_at=T + HOUR, recorded_at=T)
    assert store.situation_count() == 3  # old removed; a, b, c stay
    store.close()


def test_retention_reads_never_sweep_or_write(path):
    mono = [0.0]
    store = StateStore(path, situation_retention_s=3600, monotonic=lambda: mono[0])
    store.record_situation("old", "{}", expires_at=T - 2 * HOUR, recorded_at=T - 3 * HOUR)
    mono[0] = 1000.0
    for _ in range(3):
        assert store.get_situation("old", now=T) is None
    assert store.situation_count() == 1
    store.close()
