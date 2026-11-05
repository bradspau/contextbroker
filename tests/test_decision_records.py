import sqlite3
import pytest
from decision_records.store import RecordStore


def test_append_and_get():
    s = RecordStore(":memory:")
    rid = s.append("observation", {"a": 1})
    rec = s.get(rid)
    assert rec["id"] == rid and rec["kind"] == "observation"
    assert rec["payload"] == {"a": 1} and rec["links"] == [] and rec["created_at"]
    assert s.get("nope") is None


def test_chain_order_oldest_first():
    s = RecordStore(":memory:")
    o = s.append("observation", {})
    p = s.append("proposal", {}, links=[o])
    d = s.append("decision", {}, links=[p])
    assert [r["id"] for r in s.chain(d)] == [o, p, d]
    assert [r["kind"] for r in s.chain(d)] == ["observation", "proposal", "decision"]


def test_unknown_link_rejected():
    s = RecordStore(":memory:")
    with pytest.raises(ValueError):
        s.append("x", {}, links=["missing"])


def test_update_delete_blocked_at_db_level():
    s = RecordStore(":memory:")
    rid = s.append("x", {"a": 1})
    conn = s._conn
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE records SET kind='y' WHERE id=?", (rid,))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM records WHERE id=?", (rid,))
    assert s.get(rid)["kind"] == "x"


def test_no_mutation_api():
    s = RecordStore(":memory:")
    assert not any(hasattr(s, n) for n in ("update", "delete", "remove"))


def test_persists_to_file(tmp_path):
    p = tmp_path / "r.db"
    rid = RecordStore(p).append("k", {"z": 2})
    assert RecordStore(p).get(rid)["payload"] == {"z": 2}


def test_insert_or_replace_blocked():
    s = RecordStore(":memory:")
    rid = s.append("x", {"a": 1})
    with pytest.raises(sqlite3.DatabaseError):
        s._conn.execute(
            "INSERT OR REPLACE INTO records (id, kind, payload, links, created_at) "
            "VALUES (?, 'evil', '{}', '[]', 't')", (rid,))
    with pytest.raises(sqlite3.DatabaseError):
        s._conn.execute(
            "INSERT OR REPLACE INTO records (seq, id, kind, payload, links, created_at) "
            "VALUES (1, 'new', 'evil', '{}', '[]', 't')")
    assert s.get(rid)["kind"] == "x"


def test_find_by_payload():
    s = RecordStore(":memory:")
    a = s.append("action_execution", {"idempotency_key": "k", "status": "executed"})
    s.append("action_execution", {"idempotency_key": "j", "status": "executed"})
    s.append("other", {"idempotency_key": "k"})
    assert [r["id"] for r in s.find("action_execution", "idempotency_key", "k")] == [a]
    assert s.find("action_execution", "idempotency_key", "zz") == []


def test_thread_safe_appends():
    import threading
    s = RecordStore(":memory:")
    errs = []

    def work():
        try:
            for _ in range(50):
                s.append("k", {})
        except Exception as e:  # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errs == []
    assert s._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 400


def test_thread_safe_appends_on_a_file(tmp_path):
    import threading
    s = RecordStore(tmp_path / "threads.sqlite3")
    errs = []

    def work():
        try:
            for _ in range(50):
                s.append("k", {})
        except Exception as e:  # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errs == []
    assert s._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 400


def test_default_is_the_configured_file(tmp_path, monkeypatch):
    import os
    path = tmp_path / "records.sqlite3"
    monkeypatch.setenv("BROKER_DECISION_RECORDS_PATH", str(path))
    first = RecordStore()
    assert path.exists()
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert first._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    rid = first.append("k", {"z": 1})
    del first
    assert RecordStore().get(rid)["payload"] == {"z": 1}


def test_default_path_when_variable_unset_or_empty(monkeypatch):
    from decision_records import store
    monkeypatch.delenv("BROKER_DECISION_RECORDS_PATH", raising=False)
    assert store.resolve_path(None) == "/data/decision-records.sqlite3"
    monkeypatch.setenv("BROKER_DECISION_RECORDS_PATH", "")
    assert store.resolve_path(None) == "/data/decision-records.sqlite3"


def test_memory_only_when_a_caller_passes_it(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKER_DECISION_RECORDS_PATH", ":memory:")
    with pytest.raises(ValueError, match="BROKER_DECISION_RECORDS_PATH must be a file path"):
        RecordStore()
    monkeypatch.chdir(tmp_path)
    s = RecordStore(":memory:")
    s.append("k", {})
    assert list(tmp_path.iterdir()) == []


def test_default_argument_is_none():
    import inspect
    assert inspect.signature(RecordStore).parameters["path"].default is None


def test_append_only_after_a_reopen(tmp_path):
    p = tmp_path / "r.sqlite3"
    rid = RecordStore(p).append("x", {"a": 1})
    s = RecordStore(p)
    with pytest.raises(sqlite3.DatabaseError):
        s._conn.execute("UPDATE records SET kind='y' WHERE id=?", (rid,))
    with pytest.raises(sqlite3.DatabaseError):
        s._conn.execute("DELETE FROM records WHERE id=?", (rid,))
    with pytest.raises(sqlite3.DatabaseError):
        s._conn.execute(
            "INSERT OR REPLACE INTO records (id, kind, payload, links, created_at) "
            "VALUES (?, 'evil', '{}', '[]', 't')", (rid,))
    assert s.get(rid)["kind"] == "x"


def test_no_deletion_or_sweep_api():
    s = RecordStore(":memory:")
    assert not any(hasattr(s, n) for n in ("update", "delete", "remove", "sweep", "purge"))
