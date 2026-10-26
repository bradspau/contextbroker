import sqlite3
import pytest
from decision_records.store import RecordStore


def test_append_and_get():
    s = RecordStore()
    rid = s.append("observation", {"a": 1})
    rec = s.get(rid)
    assert rec["id"] == rid and rec["kind"] == "observation"
    assert rec["payload"] == {"a": 1} and rec["links"] == [] and rec["created_at"]
    assert s.get("nope") is None


def test_chain_order_oldest_first():
    s = RecordStore()
    o = s.append("observation", {})
    p = s.append("proposal", {}, links=[o])
    d = s.append("decision", {}, links=[p])
    assert [r["id"] for r in s.chain(d)] == [o, p, d]
    assert [r["kind"] for r in s.chain(d)] == ["observation", "proposal", "decision"]


def test_unknown_link_rejected():
    s = RecordStore()
    with pytest.raises(ValueError):
        s.append("x", {}, links=["missing"])


def test_update_delete_blocked_at_db_level():
    s = RecordStore()
    rid = s.append("x", {"a": 1})
    conn = s._conn
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE records SET kind='y' WHERE id=?", (rid,))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM records WHERE id=?", (rid,))
    assert s.get(rid)["kind"] == "x"


def test_no_mutation_api():
    s = RecordStore()
    assert not any(hasattr(s, n) for n in ("update", "delete", "remove"))


def test_persists_to_file(tmp_path):
    p = tmp_path / "r.db"
    rid = RecordStore(p).append("k", {"z": 2})
    assert RecordStore(p).get(rid)["payload"] == {"z": 2}


def test_insert_or_replace_blocked():
    s = RecordStore()
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
    s = RecordStore()
    a = s.append("action_execution", {"idempotency_key": "k", "status": "executed"})
    s.append("action_execution", {"idempotency_key": "j", "status": "executed"})
    s.append("other", {"idempotency_key": "k"})
    assert [r["id"] for r in s.find("action_execution", "idempotency_key", "k")] == [a]
    assert s.find("action_execution", "idempotency_key", "zz") == []


def test_thread_safe_appends():
    import threading
    s = RecordStore()
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
