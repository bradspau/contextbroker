"""Scope reservations so only one control loop acts on a scope at a time.

The book is safe under concurrent use within one process: one re-entrant lock covers
reserve, holder and release, so two loops can never both hold one scope. It is not
persisted: reservations are short-lived leases kept in memory, and persisting them is
deferred. Nothing in the API reaches it."""
import threading
from datetime import datetime, timedelta


def _check_aware(now) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")


class ReservationBook:
    def __init__(self):
        self._held: dict[str, tuple[str, datetime]] = {}
        # Re-entrant: reserve calls holder, and a subclass's holder runs inside the lock.
        self._lock = threading.RLock()

    def reserve(self, loop_id: str, scope: str, ttl_s: int, now: datetime) -> bool:
        _check_aware(now)
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        with self._lock:
            current = self.holder(scope, now)
            if current is not None and current != loop_id:
                return False
            self._held[scope] = (loop_id, now + timedelta(seconds=ttl_s))
            return True

    def holder(self, scope: str, now: datetime) -> str | None:
        _check_aware(now)
        with self._lock:
            entry = self._held.get(scope)
            if entry is None:
                return None
            loop_id, expires = entry
            if now >= expires:
                del self._held[scope]
                return None
            return loop_id

    def release(self, loop_id: str, scope: str) -> None:
        with self._lock:
            entry = self._held.get(scope)
            if entry is not None and entry[0] == loop_id:
                del self._held[scope]
