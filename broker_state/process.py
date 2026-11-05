"""Process-wide handle on the state store, opened lazily from the environment.

Nothing here opens a file at import. The API never keeps state in memory, so
BROKER_STATE_PATH may not name ":memory:" (D-04). A bad setting is refused
with a message that names the variable and never echoes its value.

Situation retention: a situation is reported expired from its expiry on (plan
04-04 computes that at read time) and is removed BROKER_SITUATION_RETENTION
(default 7 days) after expiry.
"""
import os
import re
import threading
from dataclasses import dataclass

from broker_engine.validation import _SAFE_RETENTION, _UNIT_SECONDS
from broker_state.store import (DEFAULT_MARK_CAP, DEFAULT_OUTCOME_CAP,
                                DEFAULT_SITUATION_CAP, StateStore)

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
_DIGITS = re.compile(r"^[0-9]+\Z")


@dataclass(frozen=True)
class StateSettings:
    path: str
    outcome_cap: int
    mark_cap: int
    situation_cap: int
    situation_retention_s: int


def _cap(environ, name: str, default: int) -> int:
    raw = environ.get(name)
    if not raw:
        return default
    if _DIGITS.match(raw) and 1 <= int(raw) <= MAX_CAP:
        return int(raw)
    raise ValueError(f"{name} must be a whole number from 1 to 1000000")


def _retention(environ) -> int:
    """Same grammar as BROKER_RETENTION_PERIOD: digits then h or d."""
    raw = environ.get(ENV_SITUATION_RETENTION) or DEFAULT_SITUATION_RETENTION
    if _SAFE_RETENTION.match(raw):
        seconds = int(raw[:-1]) * _UNIT_SECONDS[raw[-1]]
        if _MIN_RETENTION_S <= seconds <= _MAX_RETENTION_S:
            return seconds
    raise ValueError(f"{ENV_SITUATION_RETENTION} must be digits then h or d, "
                     "from 1h to 90d, for example 7d")


def load_settings(environ=os.environ) -> StateSettings:
    path = environ.get(ENV_PATH) or DEFAULT_PATH
    if path == ":memory:":
        raise ValueError(f"{ENV_PATH} must be a file path")
    return StateSettings(
        path=path,
        outcome_cap=_cap(environ, ENV_OUTCOME_CAP, DEFAULT_OUTCOME_CAP),
        mark_cap=_cap(environ, ENV_MARK_CAP, DEFAULT_MARK_CAP),
        situation_cap=_cap(environ, ENV_SITUATION_CAP, DEFAULT_SITUATION_CAP),
        situation_retention_s=_retention(environ))


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
                                situation_retention_s=settings.situation_retention_s)
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
