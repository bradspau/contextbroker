"""Process-wide handle on the state store, opened lazily from the environment.

Nothing here opens a file at import. The API never keeps state in memory, so
BROKER_STATE_PATH may not name ":memory:" (D-04). A bad setting is refused
with a message that names the variable and never echoes its value.
"""
import os
import threading
from dataclasses import dataclass

from broker_state.store import StateStore

ENV_PATH = "BROKER_STATE_PATH"
DEFAULT_PATH = "/data/broker-state.sqlite3"


@dataclass(frozen=True)
class StateSettings:
    path: str


def load_settings(environ=os.environ) -> StateSettings:
    path = environ.get(ENV_PATH) or DEFAULT_PATH
    if path == ":memory:":
        raise ValueError(f"{ENV_PATH} must be a file path")
    return StateSettings(path=path)


_LOCK = threading.Lock()
_STORE: StateStore | None = None


def get_store() -> StateStore:
    """The one store of this process, opened on first use."""
    global _STORE
    with _LOCK:
        if _STORE is None:
            settings = load_settings()
            _STORE = StateStore(settings.path)
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
