"""Append-only decision record store (sqlite3, stdlib only).

Records go to a file by default (BROKER_DECISION_RECORDS_PATH, else
/data/decision-records.sqlite3), created with mode 0600 in WAL mode. They are append-only
(triggers refuse update, replace and delete) and are never swept. Memory is used only
when a caller passes ":memory:" explicitly. Nothing in the API reaches this store."""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

ENV_PATH = "BROKER_DECISION_RECORDS_PATH"
DEFAULT_PATH = "/data/decision-records.sqlite3"
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    links TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS records_no_update BEFORE UPDATE ON records
BEGIN SELECT RAISE(ABORT, 'records is append-only'); END;
CREATE TRIGGER IF NOT EXISTS records_no_replace BEFORE INSERT ON records
WHEN EXISTS (SELECT 1 FROM records WHERE id = NEW.id OR seq = NEW.seq)
BEGIN SELECT RAISE(ABORT, 'records is append-only'); END;
CREATE TRIGGER IF NOT EXISTS records_no_delete BEFORE DELETE ON records
BEGIN SELECT RAISE(ABORT, 'records is append-only'); END;
"""


def resolve_path(path: str | Path | None) -> str:
    """An explicit path (including ":memory:") is used as given; None means the
    configured file, or the default when the variable is unset or empty."""
    if path is not None:
        return str(path)
    configured = os.environ.get(ENV_PATH, "")
    if configured == "":
        return DEFAULT_PATH
    if configured == ":memory:":
        raise ValueError(f"{ENV_PATH} must be a file path")
    return configured


class RecordStore:
    def __init__(self, path: str | Path | None = None):
        resolved = resolve_path(path)
        if resolved != ":memory:":
            os.close(os.open(resolved, os.O_CREAT | os.O_RDWR, 0o600))
        self._conn = sqlite3.connect(resolved, check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            if resolved != ":memory:":
                self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def append(self, kind: str, payload: dict, links: list[str] = []) -> str:
        links = list(links)
        with self._lock:
            for link in links:
                if self._row(link) is None:
                    raise ValueError(f"unknown linked record: {link}")
            rid = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO records (id, kind, payload, links, created_at) VALUES (?,?,?,?,?)",
                (rid, kind, json.dumps(payload), json.dumps(links),
                 datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()
            return rid

    def _row(self, record_id):
        with self._lock:
            return self._conn.execute(
                "SELECT id, kind, payload, links, created_at FROM records WHERE id=?",
                (record_id,),
            ).fetchone()

    @staticmethod
    def _to_dict(row) -> dict:
        return {"id": row[0], "kind": row[1], "payload": json.loads(row[2]),
                "links": json.loads(row[3]), "created_at": row[4]}

    def get(self, record_id: str) -> dict | None:
        row = self._row(record_id)
        return self._to_dict(row) if row else None

    def chain(self, record_id: str) -> list[dict]:
        """The record plus all transitively linked ancestors, oldest first."""
        seen: dict[str, dict] = {}
        stack = [record_id]
        while stack:
            rid = stack.pop()
            if rid in seen:
                continue
            row = self._row(rid)
            if row is None:
                continue
            seen[rid] = self._to_dict(row)
            stack.extend(seen[rid]["links"])
        with self._lock:
            order = {r[0]: r[1] for r in self._conn.execute("SELECT id, seq FROM records")}
        return sorted(seen.values(), key=lambda r: order[r["id"]])

    def find(self, kind: str, payload_key: str, payload_value) -> list[dict]:
        """Records of `kind` whose payload[payload_key] == payload_value, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, payload, links, created_at FROM records "
                "WHERE kind=? ORDER BY seq", (kind,)).fetchall()
        out = [self._to_dict(r) for r in rows]
        return [r for r in out if payload_key in r["payload"]
                and r["payload"][payload_key] == payload_value]
