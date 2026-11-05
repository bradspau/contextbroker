"""The broker's durable memory in one SQLite file (D-01).

Schema version 1 holds the last outcome per intent, the per-(intent, metric)
high-water marks and the situation records. Every write is one explicit
BEGIN IMMEDIATE transaction under one RLock, so the store stays consistent if a
second process opens the same file (D-02). A failure of SQLite or the operating
system leaving this module is StateUnavailable with a fixed text; nothing is
ever answered from memory instead (D-04).

All SQL text is literal; every value is passed as a query parameter.
"""
import functools
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from broker_engine.faults import log_fault

log = logging.getLogger(__name__)

STATE_UNAVAILABLE = "state store unavailable"
SCHEMA_VERSION = 1
DEFAULT_OUTCOME_CAP = 1024
DEFAULT_MARK_CAP = 1024
DEFAULT_SITUATION_CAP = 100000
DEFAULT_SITUATION_RETENTION_S = 604800  # 7 d
SWEEP_INTERVAL_S = 60
EVICTION_TABLES = ("outcomes", "marks", "situations")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US = timedelta(microseconds=1)
_MEMORY = ":memory:"

_SCHEMA = (
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


class StateUnavailable(Exception):
    """The state store failed. Subclasses Exception only, so no existing broad
    handler mistakes it for a client error or an upstream fault."""

    def __init__(self) -> None:
        super().__init__(STATE_UNAVAILABLE)


@dataclass(frozen=True)
class SituationRow:
    situation_id: str
    body: str
    expires_at: datetime
    first_recorded_at: datetime
    last_recorded_at: datetime
    repeat_count: int


def _to_us(dt: datetime) -> int:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return (dt - _EPOCH) // _US


def _from_us(us: int) -> datetime:
    return _EPOCH + timedelta(microseconds=us)


def _guarded(method):
    """Turn any SQLite or OS failure into StateUnavailable (class name logged only)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except (sqlite3.Error, OSError) as exc:
            log_fault(log, exc, STATE_UNAVAILABLE)
            raise StateUnavailable() from None
    return wrapper


class StateStore:
    def __init__(self, path, *, outcome_cap: int = DEFAULT_OUTCOME_CAP,
                 mark_cap: int = DEFAULT_MARK_CAP,
                 situation_cap: int = DEFAULT_SITUATION_CAP,
                 situation_retention_s: int = DEFAULT_SITUATION_RETENTION_S,
                 clock=None, monotonic=time.monotonic):
        self._caps = {"outcomes": outcome_cap, "marks": mark_cap,
                      "situations": situation_cap}
        self._retention_s = situation_retention_s
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._last_sweep: float | None = None
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        try:
            self._open(path)
        except (sqlite3.Error, OSError) as exc:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
            log_fault(log, exc, STATE_UNAVAILABLE)
            raise StateUnavailable() from None

    def _open(self, path) -> None:
        in_memory = str(path) == _MEMORY
        if not in_memory:
            # Create the file private to the broker user before SQLite does, so
            # the -wal and -shm files inherit 0600 (T-04-02).
            fd = os.open(os.fspath(path), os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(os.fspath(path), 0o600)
        self._conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False,
                                     isolation_level=None)
        conn = self._conn
        if not in_memory:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise sqlite3.OperationalError("wal unavailable")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=5000")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise sqlite3.DatabaseError("newer schema")
        if version == 0:
            with self._txn():
                # Re-read inside the write lock: another process may have created it.
                if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                    for stmt in _SCHEMA:
                        conn.execute(stmt)
                    conn.execute("PRAGMA user_version=1")  # SCHEMA_VERSION

    @contextmanager
    def _txn(self):
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @_guarded
    def close(self) -> None:
        with self._lock:
            # Keep the closed connection: any later use raises sqlite3.ProgrammingError,
            # which leaves as StateUnavailable.
            self._conn.close()

    @_guarded
    def ping(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    # -- situations ----------------------------------------------------

    @_guarded
    def record_situation(self, situation_id: str, body: str, *, expires_at: datetime,
                         recorded_at: datetime) -> None:
        expires_us = _to_us(expires_at)
        recorded_us = _to_us(recorded_at)
        with self._txn() as conn:
            conn.execute(
                "INSERT INTO situations (situation_id, expires_at_us, first_recorded_at_us, "
                "last_recorded_at_us, repeat_count, written_seq, body) VALUES "
                "(?, ?, ?, ?, 1, (SELECT COALESCE(MAX(written_seq), 0) + 1 FROM situations), ?) "
                "ON CONFLICT(situation_id) DO UPDATE SET body = excluded.body, "
                "expires_at_us = excluded.expires_at_us, "
                "last_recorded_at_us = excluded.last_recorded_at_us, "
                "repeat_count = repeat_count + 1, written_seq = excluded.written_seq",
                (situation_id, expires_us, recorded_us, recorded_us, body))

    @_guarded
    def get_situation(self, situation_id: str, *, now: datetime) -> SituationRow | None:
        _to_us(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT situation_id, body, expires_at_us, first_recorded_at_us, "
                "last_recorded_at_us, repeat_count FROM situations WHERE situation_id = ?",
                (situation_id,)).fetchone()
        if row is None:
            return None
        return SituationRow(row[0], row[1], _from_us(row[2]), _from_us(row[3]),
                            _from_us(row[4]), row[5])

    @_guarded
    def situation_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM situations").fetchone()[0]
