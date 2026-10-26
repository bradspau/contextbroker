"""Scope reservations so only one control loop acts on a scope at a time."""
from datetime import datetime, timedelta


class ReservationBook:
    def __init__(self):
        self._held: dict[str, tuple[str, datetime]] = {}

    def reserve(self, loop_id: str, scope: str, ttl_s: int, now: datetime) -> bool:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        current = self.holder(scope, now)
        if current is not None and current != loop_id:
            return False
        self._held[scope] = (loop_id, now + timedelta(seconds=ttl_s))
        return True

    def holder(self, scope: str, now: datetime) -> str | None:
        entry = self._held.get(scope)
        if entry is None:
            return None
        loop_id, expires = entry
        if now >= expires:
            del self._held[scope]
            return None
        return loop_id

    def release(self, loop_id: str, scope: str) -> None:
        entry = self._held.get(scope)
        if entry is not None and entry[0] == loop_id:
            del self._held[scope]
