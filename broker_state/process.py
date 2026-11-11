"""Process-wide handle on the state store, opened lazily from the environment.

Nothing here opens a file at import. The API never keeps state in memory, so
BROKER_STATE_PATH may not name ":memory:" (D-04). A bad setting is refused
with a message that names the variable and never echoes its value.

Situation retention: a situation is reported expired from its expiry on (plan
04-04 computes that at read time) and is removed BROKER_SITUATION_RETENTION
(default 7 days) after expiry.

Event settings (plan 05-01): BROKER_EVENT_CAP (1 to 1000000, default 100000),
BROKER_EVENT_RETENTION (digits then h or d, 1h to 90d, default 7d, counted from
ingest time), BROKER_EVENT_WINDOW_DEFAULT (default 15m) and BROKER_EVENT_WINDOW_MAX
(default 24h), both digits then s, m, h or d from 1m to 7d. The default window may
not exceed the maximum and the maximum may not exceed the retention. event_window_limits()
applies these checks; load_settings() calls it, so a bad window stops the API at start.

Promotion settings (plan 07-03): BROKER_PROMOTED_CAP (1 to 1000000, default 10000),
BROKER_PROMOTED_RETENTION (digits then h or d, 1h to 90d, default 30d, counted from the
last promotion), BROKER_PROMOTE_MIN_EVENTS (a whole number from 2 to 1000, default 3: the
distinct events a situation must show before a rule fires) and BROKER_PROMOTE_WINDOW
(digits then s, m, h or d, 1m to 7d, default 15m, not above BROKER_EVENT_RETENTION: the
span those events must fall in). promotion_thresholds() applies these; load_settings()
calls it, so a bad value stops the API at start.
"""
import os
import re
import threading
from dataclasses import dataclass

from broker_engine.validation import SAFE_RETENTION, SAFE_WINDOW, UNIT_SECONDS
from broker_state.store import (DEFAULT_EVENT_CAP, DEFAULT_MARK_CAP, DEFAULT_OUTCOME_CAP,
                                DEFAULT_PROMOTED_CAP, DEFAULT_SITUATION_CAP, StateStore)

ENV_PATH = "BROKER_STATE_PATH"
DEFAULT_PATH = "/data/broker-state.sqlite3"
ENV_OUTCOME_CAP = "BROKER_OUTCOME_CAP"
ENV_MARK_CAP = "BROKER_MARK_CAP"
ENV_SITUATION_CAP = "BROKER_SITUATION_CAP"
MAX_CAP = 1000000
ENV_SITUATION_RETENTION = "BROKER_SITUATION_RETENTION"
DEFAULT_SITUATION_RETENTION = "7d"
_MIN_RETENTION_S = 3600  # 1h
_MAX_RETENTION_S = 90 * 86400  # 90d
ENV_EVENT_CAP = "BROKER_EVENT_CAP"
ENV_EVENT_RETENTION = "BROKER_EVENT_RETENTION"
ENV_EVENT_WINDOW_DEFAULT = "BROKER_EVENT_WINDOW_DEFAULT"
ENV_EVENT_WINDOW_MAX = "BROKER_EVENT_WINDOW_MAX"
DEFAULT_EVENT_RETENTION = "7d"
DEFAULT_EVENT_WINDOW_DEFAULT = "15m"
DEFAULT_EVENT_WINDOW_MAX = "24h"
ENV_PROMOTED_CAP = "BROKER_PROMOTED_CAP"
ENV_PROMOTED_RETENTION = "BROKER_PROMOTED_RETENTION"
DEFAULT_PROMOTED_RETENTION = "30d"
ENV_PROMOTE_MIN_EVENTS = "BROKER_PROMOTE_MIN_EVENTS"
DEFAULT_PROMOTE_MIN_EVENTS = 3
MIN_PROMOTE_EVENTS = 2  # one line alone is never enough (D-57)
MAX_PROMOTE_EVENTS = 1000
ENV_PROMOTE_WINDOW = "BROKER_PROMOTE_WINDOW"
DEFAULT_PROMOTE_WINDOW = "15m"
MIN_EVENT_WINDOW_S = 60  # 1m
MAX_EVENT_WINDOW_S = 7 * 86400  # 7d
_MAX_SETTING_LEN = 20  # longer than any valid value; keeps int() off absurd digit runs
_DIGITS = re.compile(r"^[0-9]+\Z")


@dataclass(frozen=True)
class StateSettings:
    path: str
    outcome_cap: int
    mark_cap: int
    situation_cap: int
    situation_retention_s: int
    event_cap: int
    event_retention_s: int
    event_window_default_s: int
    event_window_max_s: int
    promoted_cap: int
    promoted_retention_s: int
    promote_min_events: int
    promote_window_s: int


def _cap(environ, name: str, default: int) -> int:
    raw = environ.get(name)
    if not raw:
        return default
    if _DIGITS.match(raw) and 1 <= int(raw) <= MAX_CAP:
        return int(raw)
    raise ValueError(f"{name} must be a whole number from 1 to 1000000")


def _retention(environ, name: str = ENV_SITUATION_RETENTION,
               default: str = DEFAULT_SITUATION_RETENTION) -> int:
    """Same grammar as BROKER_RETENTION_PERIOD: digits then h or d."""
    raw = environ.get(name) or default
    if len(raw) <= _MAX_SETTING_LEN and SAFE_RETENTION.match(raw):
        seconds = int(raw[:-1]) * UNIT_SECONDS[raw[-1]]
        if _MIN_RETENTION_S <= seconds <= _MAX_RETENTION_S:
            return seconds
    raise ValueError(f"{name} must be digits then h or d, from 1h to 90d, for example 7d")


def _window(environ, name: str, default: str) -> int:
    """Digits then s, m, h or d, from 1m to 7d."""
    raw = environ.get(name) or default
    if len(raw) <= _MAX_SETTING_LEN and SAFE_WINDOW.match(raw):
        seconds = int(raw[:-1]) * UNIT_SECONDS[raw[-1]]
        if MIN_EVENT_WINDOW_S <= seconds <= MAX_EVENT_WINDOW_S:
            return seconds
    raise ValueError(f"{name} must be digits then s, m, h or d, from 1m to 7d, "
                     "for example 15m")


def event_window_limits(environ=os.environ) -> tuple[int, int]:
    """(default, maximum) event window in seconds, after the cross checks: the default
    may not exceed the maximum and the maximum may not exceed the event retention."""
    default_s = _window(environ, ENV_EVENT_WINDOW_DEFAULT, DEFAULT_EVENT_WINDOW_DEFAULT)
    max_s = _window(environ, ENV_EVENT_WINDOW_MAX, DEFAULT_EVENT_WINDOW_MAX)
    retention_s = _retention(environ, ENV_EVENT_RETENTION, DEFAULT_EVENT_RETENTION)
    if default_s > max_s:
        raise ValueError(f"{ENV_EVENT_WINDOW_DEFAULT} must not exceed {ENV_EVENT_WINDOW_MAX}")
    if max_s > retention_s:
        raise ValueError(f"{ENV_EVENT_WINDOW_MAX} must not exceed {ENV_EVENT_RETENTION}")
    return default_s, max_s


def _min_events(environ) -> int:
    raw = environ.get(ENV_PROMOTE_MIN_EVENTS)
    if not raw:
        return DEFAULT_PROMOTE_MIN_EVENTS
    if _DIGITS.match(raw) and MIN_PROMOTE_EVENTS <= int(raw) <= MAX_PROMOTE_EVENTS:
        return int(raw)
    raise ValueError(f"{ENV_PROMOTE_MIN_EVENTS} must be a whole number from "
                     f"{MIN_PROMOTE_EVENTS} to {MAX_PROMOTE_EVENTS}")


def promotion_thresholds(environ=os.environ) -> tuple[int, int]:
    """(minimum distinct events, window in seconds) for the promotion rules, after the
    cross check: the window may not exceed the event retention (D-57)."""
    min_events = _min_events(environ)
    window_s = _window(environ, ENV_PROMOTE_WINDOW, DEFAULT_PROMOTE_WINDOW)
    retention_s = _retention(environ, ENV_EVENT_RETENTION, DEFAULT_EVENT_RETENTION)
    if window_s > retention_s:
        raise ValueError(f"{ENV_PROMOTE_WINDOW} must not exceed {ENV_EVENT_RETENTION}")
    return min_events, window_s


def load_settings(environ=os.environ) -> StateSettings:
    path = environ.get(ENV_PATH) or DEFAULT_PATH
    if path == ":memory:":
        raise ValueError(f"{ENV_PATH} must be a file path")
    window_default_s, window_max_s = event_window_limits(environ)
    promote_min_events, promote_window_s = promotion_thresholds(environ)
    return StateSettings(
        path=path,
        outcome_cap=_cap(environ, ENV_OUTCOME_CAP, DEFAULT_OUTCOME_CAP),
        mark_cap=_cap(environ, ENV_MARK_CAP, DEFAULT_MARK_CAP),
        situation_cap=_cap(environ, ENV_SITUATION_CAP, DEFAULT_SITUATION_CAP),
        situation_retention_s=_retention(environ),
        event_cap=_cap(environ, ENV_EVENT_CAP, DEFAULT_EVENT_CAP),
        event_retention_s=_retention(environ, ENV_EVENT_RETENTION, DEFAULT_EVENT_RETENTION),
        event_window_default_s=window_default_s,
        event_window_max_s=window_max_s,
        promoted_cap=_cap(environ, ENV_PROMOTED_CAP, DEFAULT_PROMOTED_CAP),
        promoted_retention_s=_retention(environ, ENV_PROMOTED_RETENTION,
                                        DEFAULT_PROMOTED_RETENTION),
        promote_min_events=promote_min_events,
        promote_window_s=promote_window_s)


_LOCK = threading.Lock()
_STORE: StateStore | None = None


def get_store() -> StateStore:
    """The one store of this process, opened on first use."""
    global _STORE
    with _LOCK:
        if _STORE is None:
            settings = load_settings()
            _STORE = StateStore(settings.path, outcome_cap=settings.outcome_cap,
                                mark_cap=settings.mark_cap,
                                situation_cap=settings.situation_cap,
                                situation_retention_s=settings.situation_retention_s,
                                event_cap=settings.event_cap,
                                event_retention_s=settings.event_retention_s,
                                promoted_cap=settings.promoted_cap,
                                promoted_retention_s=settings.promoted_retention_s)
        return _STORE


def open_process_store() -> StateStore:
    """The name the API start-up calls; the same store as get_store()."""
    return get_store()


def reset_store() -> None:
    """Close and forget the process store."""
    global _STORE
    with _LOCK:
        store, _STORE = _STORE, None
    if store is not None:
        store.close()
