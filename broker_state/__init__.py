"""The broker's durable memory between requests.

Holds the last outcome per intent, the per-(intent, metric) high-water marks and
the situation records in one SQLite file (D-01). One API worker is the supported
setup (D-02). A store failure is never answered from memory (D-04): it surfaces
as StateUnavailable.
"""
from broker_state.process import get_store, reset_store
from broker_state.store import StateStore, StateUnavailable

__all__ = ["StateStore", "StateUnavailable", "get_store", "reset_store"]
