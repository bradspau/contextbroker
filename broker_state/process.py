"""Process-wide handle on the state store, opened lazily from the environment.

Nothing here opens a file at import. The API never keeps state in memory, so
BROKER_STATE_PATH may not name ":memory:" (D-04). A bad setting is refused
with a message that names the variable and never echoes its value.
"""
import os
import re
import threading
from dataclasses import dataclass

from broker_state.store import (DEFAULT_MARK_CAP, DEFAULT_OUTCOME_CAP,
                                DEFAULT_SITUATION_CAP, StateStore)

ENV_PATH = "BROKER_STATE_PATH"
DEFAULT_PATH = "/data/broker-state.sqlite3"
ENV_OUTCOME_CAP = "BROKER_OUTCOME_CAP"
ENV_MARK_CAP = "BROKER_MARK_CAP"
ENV_SITUATION_CAP = "BROKER_SITUATION_CAP"
MAX_CAP = 1000000
_DIGITS = re.compile(r"^[0-9]+\Z")


@dataclass(frozen=True)
class StateSettings:
    path: str
    outcome_cap: int
    mark_cap: int
    situation_cap: int


def _cap(environ, name: str, default: int) -> int:
    raw = environ.get(name)
    if not raw:
        return default
    if _DIGITS.match(raw) and 1 <= int(raw) <= MAX_CAP:
        return int(raw)
    raise ValueError(f"{name} must be a whole number from 1 to 1000000")


def load_settings(environ=os.environ) -> StateSettings:
    path = environ.get(ENV_PATH) or DEFAULT_PATH
    if path == ":memory:":
        raise ValueError(f"{ENV_PATH} must be a file path")
    return StateSettings(
        path=path,
        outcome_cap=_cap(environ, ENV_OUTCOME_CAP, DEFAULT_OUTCOME_CAP),
        mark_cap=_cap(environ, ENV_MARK_CAP, DEFAULT_MARK_CAP),
        situation_cap=_cap(environ, ENV_SITUATION_CAP, DEFAULT_SITUATION_CAP))


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
                                situation_cap=settings.situation_cap)
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
