"""The broker's durable memory in one SQLite file (D-01).

Situation retention (D-09, D-10): a situation is reported expired from its expiry
on (the caller computes that at read time) and is hidden by get_situation once
its expiry plus the retention period (default 7 d) has passed. It is physically
removed by a sweep that runs inside a situation write, at most once a minute,
never from a read and never in a background thread; it touches the situations
table only, never outcomes or marks (D-14).

Schema version 1 holds the last outcome per intent, the per-(intent, metric)
high-water marks and the situation records, plus the counters. Phase 5 adds the
events and event_devices tables at the same version (D-34): user_version changes
only when an existing table changes shape or meaning; new tables are added with
IF NOT EXISTS, so the Phase 4 image still opens the file. Every write is one explicit
BEGIN IMMEDIATE transaction under one RLock, so the store stays consistent if a
second process opens the same file (D-02). A failure of SQLite or the operating
system leaving this module is StateUnavailable with a fixed text; nothing is
ever answered from memory instead (D-04).

Events (D-03, D-06): never updated. At the cap (default 100000) the oldest events
by write order are evicted together with their event_devices rows, counted in
evicted_events and announced by one line, "state eviction: table=events evicted=N".
A sweep inside an event write, at most once a minute on its own timer, removes
events by ingest time once the retention (default 7 d) has passed, announced by
"state retention: table=events removed=N" when it removed any. The sweep ages by
ingest time only, since an event time comes from the device and cannot be trusted
(a nil or future one would otherwise pin an event for ever). Reads hide events past
retention without waiting for a sweep. Neither line carries a host, device or id.

Promoted records (D-59, D-60, D-66, D-67): one more table, promoted, added at the same
schema version with IF NOT EXISTS, by its own existence check on any existing version-1
file. A row is derived semantic state (a promoted conflict or a hypothesis) with its
valid-from time and the ids of its support: the stored situation and at most 20 events.
It is written only by the operator command (plan 07-04), never by a request. A re-run
is an upsert that keeps the earliest valid-from. At the cap (default 10000) the least
recently written rows are evicted inside the write, counted in counters as
evicted_promoted (not in EVICTION_TABLES, so /health keeps its shape) and announced by
"state eviction: table=promoted evicted=N". A sweep inside a promotion write, at most
once a minute on its own timer, removes rows BROKER_PROMOTED_RETENTION (default 30 d)
after their last promotion; reads hide them without waiting. support_present() says
whether the situation and each event of a record still exist, so a record never points
at an evicted source without saying so.

All SQL text is literal; every value is passed as a query parameter.
"""
import functools
import json
import logging
import math
import os
import re
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
EVICTION_TABLES = ("outcomes", "marks", "situations", "events")
DEFAULT_EVENT_CAP = 100000
DEFAULT_EVENT_RETENTION_S = 604800  # 7 d
EVENT_BATCH_MAX = 500
QUARANTINE_REASONS = ("no priority", "not text", "too long", "unparsable header")
EVENT_FORMATS = ("rfc5424", "rfc3164")
EVENT_MAPPINGS = ("mapped", "ambiguous", "unmapped")
EVENT_LIMIT_MAX = 10000
DEFAULT_PROMOTED_CAP = 10000
DEFAULT_PROMOTED_RETENTION_S = 2592000  # 30 d
PROMOTED_KINDS = ("conflict", "hypothesis")
PROMOTED_SUPPORT_MAX = 20
PROMOTED_BATCH_MAX = 200
PROMOTED_LIMIT_MAX = 1000
PROMOTED_BODY_MAX = 4096
PROMOTED_EVICTION_COUNTER = "evicted_promoted"

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

# Added at schema version 1 (D-34). Events are never updated; they leave only by the
# cap or the retention sweep. event_devices holds one row per device the event may
# belong to: one for mapped, one per candidate for ambiguous, none for unmapped.
_EVENT_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS events ("
    "event_id TEXT PRIMARY KEY, written_seq INTEGER NOT NULL, "
    "ingest_time_us INTEGER NOT NULL, event_time_us INTEGER, "
    "placed_at_us INTEGER NOT NULL, "
    "severity INTEGER NOT NULL CHECK (severity BETWEEN 0 AND 7), "
    "facility INTEGER NOT NULL CHECK (facility BETWEEN 0 AND 23), "
    "format TEXT NOT NULL CHECK (format IN ('rfc5424','rfc3164')), "
    "source_host TEXT NOT NULL, "
    "mapping TEXT NOT NULL CHECK (mapping IN ('mapped','ambiguous','unmapped')), "
    "device TEXT, kind TEXT, raw TEXT NOT NULL, "
    "CHECK ((mapping = 'mapped') = (device IS NOT NULL)))",
    "CREATE INDEX IF NOT EXISTS events_written_seq ON events (written_seq)",
    "CREATE INDEX IF NOT EXISTS events_ingest_time ON events (ingest_time_us)",
    "CREATE INDEX IF NOT EXISTS events_placed_at ON events (placed_at_us)",
    "CREATE TABLE IF NOT EXISTS event_devices ("
    "event_id TEXT NOT NULL, device TEXT NOT NULL, PRIMARY KEY (event_id, device))",
    "CREATE INDEX IF NOT EXISTS event_devices_device ON event_devices (device, event_id)",
)

# Added at schema version 1 (D-59). Written only by the operator command. event_ids is a
# JSON array of at most PROMOTED_SUPPORT_MAX ids; device_b is set for a hypothesis only.
_PROMOTED_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS promoted ("
    "promoted_id TEXT PRIMARY KEY, "
    "kind TEXT NOT NULL CHECK (kind IN ('conflict','hypothesis')), "
    "rule TEXT NOT NULL, device_a TEXT NOT NULL, device_b TEXT, body TEXT NOT NULL, "
    "valid_from_us INTEGER NOT NULL, promoted_at_us INTEGER NOT NULL, "
    "last_promoted_at_us INTEGER NOT NULL, repeat_count INTEGER NOT NULL, "
    "written_seq INTEGER NOT NULL, situation_id TEXT NOT NULL, "
    "situation_requester TEXT NOT NULL, event_ids TEXT NOT NULL, "
    "CHECK ((kind = 'conflict') = (device_b IS NULL)))",
    "CREATE INDEX IF NOT EXISTS promoted_written_seq ON promoted (written_seq)",
    "CREATE INDEX IF NOT EXISTS promoted_device_a ON promoted (device_a, kind)",
    "CREATE INDEX IF NOT EXISTS promoted_device_b ON promoted (device_b, kind)",
    "CREATE INDEX IF NOT EXISTS promoted_last ON promoted (last_promoted_at_us)",
)

_OUTCOMES = ("success", "failure", "inconclusive")

# Literal statements per table; the table name is never interpolated into SQL.
_COUNT_SQL = {
    "outcomes": "SELECT COUNT(*) FROM outcomes",
    "marks": "SELECT COUNT(*) FROM marks",
    "situations": "SELECT COUNT(*) FROM situations",
    "events": "SELECT COUNT(*) FROM events",
}
_EVICT_SQL = {
    "outcomes": "DELETE FROM outcomes WHERE rowid IN "
                "(SELECT rowid FROM outcomes ORDER BY written_seq LIMIT ?)",
    "marks": "DELETE FROM marks WHERE rowid IN "
             "(SELECT rowid FROM marks ORDER BY written_seq LIMIT ?)",
    "situations": "DELETE FROM situations WHERE rowid IN "
                  "(SELECT rowid FROM situations ORDER BY written_seq LIMIT ?)",
}
_COUNTER_NAME = {"outcomes": "evicted_outcomes", "marks": "evicted_marks",
                 "situations": "evicted_situations", "events": "evicted_events"}
_QUARANTINE_COUNTER = {"no priority": "quarantine_no_priority",
                       "not text": "quarantine_not_text",
                       "too long": "quarantine_too_long",
                       "unparsable header": "quarantine_unparsable_header"}

_EVENT_ID = re.compile(r"^evt-[0-9a-f]{32}\Z")
_DEVICE = re.compile(r"^[A-Za-z0-9_\-/]{1,128}\Z")
_KIND = re.compile(r"^[a-z_]{1,32}\Z")
_PROMOTED_ID = re.compile(r"^prm-[0-9a-f]{32}\Z")
_PROMOTED_RULE = re.compile(r"^[a-z_]{1,64}\Z")
_SITUATION_ID = re.compile(r"^sit-[0-9a-f]{32}\Z")
_UPSERT_COUNTER = ("INSERT INTO counters (name, value) VALUES (?, ?) "
                   "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value")


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


@dataclass(frozen=True)
class NewEvent:
    """One event as ingest hands it to the store. add_events validates it."""
    event_id: str
    ingest_time: datetime
    event_time: datetime | None
    severity: int
    facility: int
    format: str
    source_host: str
    mapping: str
    device: str | None
    candidates: tuple
    kind: str | None
    raw: str


@dataclass(frozen=True)
class EventRow:
    """An event as a read returns it. The raw line never leaves through a window read."""
    event_id: str
    ingest_time: datetime
    event_time: datetime | None
    severity: int
    facility: int
    format: str
    source_host: str
    mapping: str
    device: str | None
    candidates: tuple
    kind: str | None


@dataclass(frozen=True)
class EventBatch:
    """What one add_events call did. unmapped and ambiguous count newly stored rows."""
    stored: int
    duplicates: int
    unmapped: int
    ambiguous: int


@dataclass(frozen=True)
class NewPromoted:
    """One promoted record as the operator command hands it to the store."""
    promoted_id: str
    kind: str
    rule: str
    device_a: str
    device_b: str | None
    body: str
    valid_from: datetime
    situation_id: str
    situation_requester: str
    event_ids: tuple


@dataclass(frozen=True)
class PromotedRow:
    """A promoted record as a read returns it."""
    promoted_id: str
    kind: str
    rule: str
    device_a: str
    device_b: str | None
    body: str
    valid_from: datetime
    promoted_at: datetime
    last_promoted_at: datetime
    repeat_count: int
    situation_id: str
    situation_requester: str
    event_ids: tuple


@dataclass(frozen=True)
class PromoteBatch:
    """What one promote call did: new ids, refreshed ids, rows the cap evicted and rows
    the retention sweep removed."""
    written: int
    refreshed: int
    evicted: int
    removed: int


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_device(value) -> None:
    if not isinstance(value, str) or not _DEVICE.match(value):
        raise ValueError("device must be 1 to 128 letters, digits, _, - or /")


def _check_event(e) -> None:
    """Every refusal here is a programming error in the caller, raised before any SQL."""
    if not isinstance(e, NewEvent):
        raise ValueError("event must be a NewEvent")
    if not isinstance(e.event_id, str) or not _EVENT_ID.match(e.event_id):
        raise ValueError("event_id must be evt- and 32 lower-case hex digits")
    _to_us(e.ingest_time)
    if e.event_time is not None:
        _to_us(e.event_time)
    if not _is_int(e.severity) or not 0 <= e.severity <= 7:
        raise ValueError("severity must be a whole number from 0 to 7")
    if not _is_int(e.facility) or not 0 <= e.facility <= 23:
        raise ValueError("facility must be a whole number from 0 to 23")
    if e.format not in EVENT_FORMATS:
        raise ValueError("format must be rfc5424 or rfc3164")
    if e.mapping not in EVENT_MAPPINGS:
        raise ValueError("mapping must be mapped, ambiguous or unmapped")
    host = e.source_host
    if (not isinstance(host, str) or not 1 <= len(host) <= 255
            or any(not 33 <= ord(c) <= 126 for c in host)):
        raise ValueError("source_host must be 1 to 255 printable characters without spaces")
    cands = e.candidates
    if not isinstance(cands, tuple):
        raise ValueError("candidates must be a tuple")
    for c in cands:
        _check_device(c)
    if e.device is not None:
        _check_device(e.device)
    if e.mapping == "mapped":
        if e.device is None or cands != (e.device,):
            raise ValueError("a mapped event has a device and exactly that candidate")
    elif e.device is not None:
        raise ValueError("only a mapped event has a device")
    elif e.mapping == "ambiguous":
        if len(cands) < 2 or list(cands) != sorted(set(cands)):
            raise ValueError("an ambiguous event has two or more sorted distinct candidates")
    elif cands:
        raise ValueError("an unmapped event has no candidates")
    if e.kind is not None and (not isinstance(e.kind, str) or not _KIND.match(e.kind)):
        raise ValueError("kind must be 1 to 32 lower-case letters or underscores")
    if not isinstance(e.raw, str) or not e.raw:
        raise ValueError("raw must be a non-empty string")
    try:
        e.raw.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("raw must be encodable as UTF-8") from None


def _reject_constant(_name):
    raise ValueError("body must be a JSON object")


def _check_promoted(r) -> None:
    """Every refusal here is a programming error in the caller, raised before any SQL."""
    if not isinstance(r, NewPromoted):
        raise ValueError("record must be a NewPromoted")
    if not isinstance(r.promoted_id, str) or not _PROMOTED_ID.match(r.promoted_id):
        raise ValueError("promoted_id must be prm- and 32 lower-case hex digits")
    if r.kind not in PROMOTED_KINDS:
        raise ValueError("kind must be conflict or hypothesis")
    if not isinstance(r.rule, str) or not _PROMOTED_RULE.match(r.rule):
        raise ValueError("rule must be 1 to 64 lower-case letters or underscores")
    _check_device(r.device_a)
    if r.device_b is not None:
        _check_device(r.device_b)
    if (r.kind == "conflict") != (r.device_b is None):
        raise ValueError("only a hypothesis has a second device")
    if not isinstance(r.body, str) or len(r.body) > PROMOTED_BODY_MAX:
        raise ValueError("body must be a string of at most %d characters" % PROMOTED_BODY_MAX)
    try:
        parsed = json.loads(r.body, parse_constant=_reject_constant)
    except ValueError:
        raise ValueError("body must be a JSON object") from None
    if not isinstance(parsed, dict):
        raise ValueError("body must be a JSON object")
    _to_us(r.valid_from)
    if not isinstance(r.situation_id, str) or not _SITUATION_ID.match(r.situation_id):
        raise ValueError("situation_id must be sit- and 32 lower-case hex digits")
    who = r.situation_requester
    if not isinstance(who, str) or not 1 <= len(who) <= 1024 or not who.isprintable():
        raise ValueError("situation_requester must be 1 to 1024 printable characters")
    ids = r.event_ids
    if not isinstance(ids, tuple) or not 1 <= len(ids) <= PROMOTED_SUPPORT_MAX:
        raise ValueError("event_ids must be a tuple of 1 to %d ids" % PROMOTED_SUPPORT_MAX)
    for i in ids:
        if not isinstance(i, str) or not _EVENT_ID.match(i):
            raise ValueError("event_id must be evt- and 32 lower-case hex digits")
    if len(set(ids)) != len(ids):
        raise ValueError("event_ids must be distinct")


def _check_quarantine(quarantined) -> dict:
    if quarantined is None:
        return {}
    if not isinstance(quarantined, dict):
        raise ValueError("quarantined must be a mapping of reason to count")
    for reason, n in quarantined.items():
        if reason not in QUARANTINE_REASONS:
            raise ValueError("unknown quarantine reason")
        if not _is_int(n) or n < 0:
            raise ValueError("a quarantine count must be a non-negative whole number")
    return quarantined


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
                 event_cap: int = DEFAULT_EVENT_CAP,
                 event_retention_s: int = DEFAULT_EVENT_RETENTION_S,
                 promoted_cap: int = DEFAULT_PROMOTED_CAP,
                 promoted_retention_s: int = DEFAULT_PROMOTED_RETENTION_S,
                 clock=None, monotonic=time.monotonic):
        self._caps = {"outcomes": outcome_cap, "marks": mark_cap,
                      "situations": situation_cap, "events": event_cap}
        self._promoted_cap = promoted_cap
        self._retention_s = situation_retention_s
        self._event_retention_s = event_retention_s
        self._promoted_retention_s = promoted_retention_s
        self._last_promoted_sweep: float | None = None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._last_sweep: float | None = None
        self._last_event_sweep: float | None = None
        self._after: list = []
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

    @property
    def event_retention_s(self) -> int:
        return self._event_retention_s

    @property
    def promoted_retention_s(self) -> int:
        return self._promoted_retention_s

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
                    for stmt in _SCHEMA + _EVENT_SCHEMA + _PROMOTED_SCHEMA:
                        conn.execute(stmt)
                    conn.execute("PRAGMA user_version=1")  # SCHEMA_VERSION
        elif not self._has_event_tables():
            # A Phase 4 file: add the event tables, version unchanged (D-34).
            with self._txn():
                if not self._has_event_tables():  # re-read inside the write lock
                    for stmt in _EVENT_SCHEMA:
                        conn.execute(stmt)
        # Any version-1 file may lack the promoted table (Phase 4, 5 or 6): the check
        # above only fires for missing event tables, so this one runs on its own (D-59).
        if version != 0 and not self._has_promoted_table():
            with self._txn():
                if not self._has_promoted_table():  # re-read inside the write lock
                    for stmt in _PROMOTED_SCHEMA:
                        conn.execute(stmt)

    def _has_event_tables(self) -> bool:
        return self._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('events', 'event_devices')").fetchone()[0] == 2

    def _has_promoted_table(self) -> bool:
        return self._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name = 'promoted'").fetchone()[0] == 1

    @contextmanager
    def _txn(self):
        """One BEGIN IMMEDIATE transaction under the lock. Log lines queued with
        self._after are written only after COMMIT; a rolled-back write logs nothing."""
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            self._after = []
            try:
                yield conn
            except BaseException:
                self._after = []
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            queued, self._after = self._after, []
        for fmt, args in queued:
            log.warning(fmt, *args)

    def _make_room(self, conn, table: str, exists: bool) -> None:
        """Before a new key is written to a full table, drop the least recently
        written rows, count them and queue one fixed log line (D-11, D-12)."""
        if exists:
            return
        count = conn.execute(_COUNT_SQL[table]).fetchone()[0]
        cap = self._caps[table]
        if count < cap:
            return
        n = count - cap + 1
        conn.execute(_EVICT_SQL[table], (n,))
        conn.execute(
            "INSERT INTO counters (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
            (_COUNTER_NAME[table], n))
        self._after.append(("state eviction: table=%s evicted=%d", (table, n)))

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
        swept_at = None
        with self._txn() as conn:
            exists = conn.execute("SELECT 1 FROM situations WHERE situation_id = ?",
                                  (situation_id,)).fetchone() is not None
            self._make_room(conn, "situations", exists)
            conn.execute(
                "INSERT INTO situations (situation_id, expires_at_us, first_recorded_at_us, "
                "last_recorded_at_us, repeat_count, written_seq, body) VALUES "
                "(?, ?, ?, ?, 1, (SELECT COALESCE(MAX(written_seq), 0) + 1 FROM situations), ?) "
                "ON CONFLICT(situation_id) DO UPDATE SET body = excluded.body, "
                "expires_at_us = excluded.expires_at_us, "
                "last_recorded_at_us = excluded.last_recorded_at_us, "
                "repeat_count = repeat_count + 1, written_seq = excluded.written_seq",
                (situation_id, expires_us, recorded_us, recorded_us, body))
            tick = self._monotonic()
            if self._last_sweep is None or tick - self._last_sweep >= SWEEP_INTERVAL_S:
                removed = conn.execute(
                    "DELETE FROM situations WHERE expires_at_us + ? <= ?",
                    (self._retention_s * 1000000, recorded_us)).rowcount
                swept_at = tick
                if removed > 0:
                    self._after.append(
                        ("state retention: table=situations removed=%d", (removed,)))
        if swept_at is not None:  # taken only after COMMIT
            self._last_sweep = swept_at

    @_guarded
    def get_situation(self, situation_id: str, *, now: datetime) -> SituationRow | None:
        now_us = _to_us(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT situation_id, body, expires_at_us, first_recorded_at_us, "
                "last_recorded_at_us, repeat_count FROM situations WHERE situation_id = ?",
                (situation_id,)).fetchone()
        if row is None or row[2] + self._retention_s * 1000000 <= now_us:
            return None  # read-side cut-off: the stated policy holds without a sweep
        return SituationRow(row[0], row[1], _from_us(row[2]), _from_us(row[3]),
                            _from_us(row[4]), row[5])

    @_guarded
    def situation_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM situations").fetchone()[0]

    # -- outcomes ------------------------------------------------------

    @_guarded
    def last_outcome(self, intent_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT outcome FROM outcomes WHERE intent_id = ?",
                                     (intent_id,)).fetchone()
        return row[0] if row else None

    @_guarded
    def record_outcome(self, intent_id: str, outcome: str) -> None:
        if outcome not in _OUTCOMES:
            raise ValueError("outcome must be success, failure or inconclusive")
        with self._txn() as conn:
            exists = conn.execute("SELECT 1 FROM outcomes WHERE intent_id = ?",
                                  (intent_id,)).fetchone() is not None
            self._make_room(conn, "outcomes", exists)
            conn.execute(
                "INSERT INTO outcomes (intent_id, outcome, written_at_us, written_seq) "
                "VALUES (?, ?, ?, (SELECT COALESCE(MAX(written_seq), 0) + 1 FROM outcomes)) "
                "ON CONFLICT(intent_id) DO UPDATE SET outcome = excluded.outcome, "
                "written_at_us = excluded.written_at_us, written_seq = excluded.written_seq",
                (intent_id, outcome, _to_us(self._clock())))

    @_guarded
    def outcome_count(self) -> int:
        with self._lock:
            return self._conn.execute(_COUNT_SQL["outcomes"]).fetchone()[0]

    # -- high-water marks ----------------------------------------------

    @_guarded
    def marks(self, intent_id: str, metric_uris) -> dict[str, tuple[datetime, float]]:
        out: dict[str, tuple[datetime, float]] = {}
        with self._lock:
            for uri in metric_uris:
                row = self._conn.execute(
                    "SELECT observed_at_us, value FROM marks "
                    "WHERE intent_id = ? AND metric_uri = ?", (intent_id, uri)).fetchone()
                if row is not None:
                    out[uri] = (_from_us(row[0]), row[1])
        return out

    @_guarded
    def advance_mark(self, intent_id: str, metric_uri: str, observed_at: datetime,
                     value: float) -> bool:
        """Move the mark to (observed_at, value) unless that would go backwards.

        One BEGIN IMMEDIATE transaction, so a second process cannot write between
        the check and the advance (D-02). The refusal rule is the one the TMF921
        path always had: refuse unless there is no mark, the time is later, or the
        time is equal and the value is equal. Returns whether the mark was written.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("value must be a finite number")
        try:
            number = float(value)
        except OverflowError:
            raise ValueError("value must be a finite number") from None
        if not math.isfinite(number):
            raise ValueError("value must be a finite number")
        observed_us = _to_us(observed_at)
        with self._txn() as conn:
            row = conn.execute(
                "SELECT observed_at_us, value FROM marks "
                "WHERE intent_id = ? AND metric_uri = ?", (intent_id, metric_uri)).fetchone()
            if row is not None and not (observed_us > row[0]
                                        or (observed_us == row[0] and number == row[1])):
                return False
            self._make_room(conn, "marks", row is not None)
            conn.execute(
                "INSERT INTO marks (intent_id, metric_uri, observed_at_us, value, "
                "written_at_us, written_seq) VALUES (?, ?, ?, ?, ?, "
                "(SELECT COALESCE(MAX(written_seq), 0) + 1 FROM marks)) "
                "ON CONFLICT(intent_id, metric_uri) DO UPDATE SET "
                "observed_at_us = excluded.observed_at_us, value = excluded.value, "
                "written_at_us = excluded.written_at_us, written_seq = excluded.written_seq",
                (intent_id, metric_uri, observed_us, number, _to_us(self._clock())))
            return True

    @_guarded
    def mark_count(self) -> int:
        with self._lock:
            return self._conn.execute(_COUNT_SQL["marks"]).fetchone()[0]

    # -- events --------------------------------------------------------

    @_guarded
    def add_events(self, events, *, quarantined=None) -> EventBatch:
        """Store a batch of events and count its duplicates, unmapped events and
        quarantined lines, all in one transaction (D-05, D-21).

        A line already stored (same event_id) is not stored again and adds one to
        the duplicates counter. Events are never updated. Anything a caller got wrong
        is a ValueError raised before any SQL."""
        events = list(events)
        if len(events) > EVENT_BATCH_MAX:
            raise ValueError("a batch holds at most %d events" % EVENT_BATCH_MAX)
        for e in events:
            _check_event(e)
        quarantined = _check_quarantine(quarantined)
        stored = duplicates = unmapped = ambiguous = 0
        swept_at = None
        with self._txn() as conn:
            seq = conn.execute("SELECT COALESCE(MAX(written_seq), 0) FROM events").fetchone()[0]
            for e in events:
                ingest_us = _to_us(e.ingest_time)
                event_us = None if e.event_time is None else _to_us(e.event_time)
                placed_us = ingest_us if event_us is None else event_us
                cur = conn.execute(
                    "INSERT INTO events (event_id, written_seq, ingest_time_us, "
                    "event_time_us, placed_at_us, severity, facility, format, source_host, "
                    "mapping, device, kind, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(event_id) DO NOTHING",
                    (e.event_id, seq + 1, ingest_us, event_us, placed_us, e.severity,
                     e.facility, e.format, e.source_host, e.mapping, e.device, e.kind, e.raw))
                if cur.rowcount == 0:
                    duplicates += 1
                    continue
                seq += 1
                stored += 1
                for candidate in e.candidates:
                    conn.execute("INSERT INTO event_devices (event_id, device) VALUES (?, ?)",
                                 (e.event_id, candidate))
                if e.mapping == "unmapped":
                    unmapped += 1
                elif e.mapping == "ambiguous":
                    ambiguous += 1
            counts = [("events_duplicates", duplicates), ("events_unmapped", unmapped)]
            counts += [(_QUARANTINE_COUNTER[reason], quarantined.get(reason, 0))
                       for reason in QUARANTINE_REASONS]
            for name, n in counts:
                if n > 0:
                    conn.execute(_UPSERT_COUNTER, (name, n))
            tick = self._monotonic()
            if (self._last_event_sweep is None
                    or tick - self._last_event_sweep >= SWEEP_INTERVAL_S):
                cutoff = (self._event_retention_s * 1000000, _to_us(self._clock()))
                conn.execute(
                    "DELETE FROM event_devices WHERE event_id IN "
                    "(SELECT event_id FROM events WHERE ingest_time_us + ? <= ?)", cutoff)
                removed = conn.execute(
                    "DELETE FROM events WHERE ingest_time_us + ? <= ?", cutoff).rowcount
                swept_at = tick
                if removed > 0:
                    self._after.append(
                        ("state retention: table=events removed=%d", (removed,)))
            if stored > 0:  # one count per batch, not one per row
                count = conn.execute(_COUNT_SQL["events"]).fetchone()[0]
                excess = count - self._caps["events"]
                if excess > 0:
                    conn.execute(
                        "DELETE FROM event_devices WHERE event_id IN "
                        "(SELECT event_id FROM events ORDER BY written_seq LIMIT ?)",
                        (excess,))
                    conn.execute(
                        "DELETE FROM events WHERE rowid IN "
                        "(SELECT rowid FROM events ORDER BY written_seq LIMIT ?)", (excess,))
                    conn.execute(_UPSERT_COUNTER, (_COUNTER_NAME["events"], excess))
                    self._after.append(("state eviction: table=events evicted=%d", (excess,)))
        if swept_at is not None:  # taken only after COMMIT
            self._last_event_sweep = swept_at
        return EventBatch(stored=stored, duplicates=duplicates, unmapped=unmapped,
                          ambiguous=ambiguous)

    @_guarded
    def events_in_window(self, device: str, *, start: datetime, end: datetime,
                         known_at: datetime | None, now: datetime,
                         limit: int) -> list[EventRow]:
        """Events that name `device`, placed (event time, else ingest time) in
        [start, end], ingested at or before known_at, and not past retention at `now`.
        Newest placed first, then event_id. An unmapped event is never returned."""
        _check_device(device)
        start_us, end_us, now_us = _to_us(start), _to_us(end), _to_us(now)
        known_us = None if known_at is None else _to_us(known_at)
        if start_us > end_us:
            raise ValueError("start must not be after end")
        if not _is_int(limit) or not 1 <= limit <= EVENT_LIMIT_MAX:
            raise ValueError("limit must be a whole number from 1 to %d" % EVENT_LIMIT_MAX)
        out: list[EventRow] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT e.event_id, e.ingest_time_us, e.event_time_us, e.severity, "
                "e.facility, e.format, e.source_host, e.mapping, e.device, e.kind "
                "FROM events e JOIN event_devices d ON d.event_id = e.event_id "
                "WHERE d.device = ? AND e.placed_at_us >= ? AND e.placed_at_us <= ? "
                "AND (? IS NULL OR e.ingest_time_us <= ?) "
                "AND e.ingest_time_us + ? > ? "
                "ORDER BY e.placed_at_us DESC, e.event_id ASC LIMIT ?",
                (device, start_us, end_us, known_us, known_us,
                 self._event_retention_s * 1000000, now_us, limit)).fetchall()
            for r in rows:
                cands = tuple(c[0] for c in self._conn.execute(
                    "SELECT device FROM event_devices WHERE event_id = ? ORDER BY device",
                    (r[0],)).fetchall())
                out.append(EventRow(
                    event_id=r[0], ingest_time=_from_us(r[1]),
                    event_time=None if r[2] is None else _from_us(r[2]),
                    severity=r[3], facility=r[4], format=r[5], source_host=r[6],
                    mapping=r[7], device=r[8], candidates=cands, kind=r[9]))
        return out

    @_guarded
    def event_raw(self, event_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT raw FROM events WHERE event_id = ?",
                                     (event_id,)).fetchone()
        return row[0] if row else None

    @_guarded
    def event_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    @_guarded
    def event_counters(self) -> dict:
        with self._lock:
            rows = dict(self._conn.execute("SELECT name, value FROM counters").fetchall())
        return {"duplicates": rows.get("events_duplicates", 0),
                "unmapped": rows.get("events_unmapped", 0),
                "quarantined": {r: rows.get(_QUARANTINE_COUNTER[r], 0)
                                for r in QUARANTINE_REASONS}}

    # -- promoted records ----------------------------------------------

    @_guarded
    def promote(self, records, *, now: datetime) -> PromoteBatch:
        """Write promoted records in one transaction (D-59, D-67).

        Anything a caller got wrong is a ValueError raised before any SQL."""
        records = list(records)
        if len(records) > PROMOTED_BATCH_MAX:
            raise ValueError("a batch holds at most %d records" % PROMOTED_BATCH_MAX)
        for r in records:
            _check_promoted(r)
        if len({r.promoted_id for r in records}) != len(records):
            raise ValueError("a batch holds each promoted_id once")
        now_us = _to_us(now)
        written = 0
        with self._txn() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(written_seq), 0) FROM promoted").fetchone()[0]
            for r in records:
                seq += 1
                conn.execute(
                    "INSERT INTO promoted (promoted_id, kind, rule, device_a, device_b, "
                    "body, valid_from_us, promoted_at_us, last_promoted_at_us, "
                    "repeat_count, written_seq, situation_id, situation_requester, "
                    "event_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (r.promoted_id, r.kind, r.rule, r.device_a, r.device_b, r.body,
                     _to_us(r.valid_from), now_us, now_us, seq, r.situation_id,
                     r.situation_requester, json.dumps(list(r.event_ids))))
                written += 1
        return PromoteBatch(written=written, refreshed=0, evicted=0, removed=0)

    @_guarded
    def promoted_rows(self, kind: str, device: str, *, valid_at: datetime,
                      known_at: datetime | None, now: datetime,
                      limit: int) -> list[PromotedRow]:
        """Records of `kind` that name `device` as device_a or device_b, valid at
        `valid_at`, promoted at or before known_at (when given) and not past retention
        at `now`. Newest valid-from first, then promoted_id."""
        if kind not in PROMOTED_KINDS:
            raise ValueError("kind must be conflict or hypothesis")
        _check_device(device)
        valid_us, now_us = _to_us(valid_at), _to_us(now)
        known_us = None if known_at is None else _to_us(known_at)
        if not _is_int(limit) or not 1 <= limit <= PROMOTED_LIMIT_MAX:
            raise ValueError("limit must be a whole number from 1 to %d" % PROMOTED_LIMIT_MAX)
        with self._lock:
            rows = self._conn.execute(
                "SELECT promoted_id, kind, rule, device_a, device_b, body, valid_from_us, "
                "promoted_at_us, last_promoted_at_us, repeat_count, situation_id, "
                "situation_requester, event_ids FROM promoted "
                "WHERE kind = ? AND (device_a = ? OR device_b = ?) "
                "AND valid_from_us <= ? AND last_promoted_at_us + ? > ? "
                "AND (? IS NULL OR promoted_at_us <= ?) "
                "ORDER BY valid_from_us DESC, promoted_id ASC LIMIT ?",
                (kind, device, device, valid_us, self._promoted_retention_s * 1000000,
                 now_us, known_us, known_us, limit)).fetchall()
        return [PromotedRow(
            promoted_id=r[0], kind=r[1], rule=r[2], device_a=r[3], device_b=r[4],
            body=r[5], valid_from=_from_us(r[6]), promoted_at=_from_us(r[7]),
            last_promoted_at=_from_us(r[8]), repeat_count=r[9], situation_id=r[10],
            situation_requester=r[11], event_ids=tuple(json.loads(r[12]))) for r in rows]

    @_guarded
    def promoted_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM promoted").fetchone()[0]

    # -- eviction counters ---------------------------------------------

    @_guarded
    def evictions(self) -> dict[str, int]:
        with self._lock:
            rows = dict(self._conn.execute("SELECT name, value FROM counters").fetchall())
        return {t: rows.get(_COUNTER_NAME[t], 0) for t in EVICTION_TABLES}
