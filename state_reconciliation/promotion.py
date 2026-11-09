"""The rule that selects a reconciliation conflict for promotion (SEM-01, D-56, D-57, D-60).

The rule ``repeated_link_down_vs_configured`` in words: a conflict is selected for a device when at
least ``min_events`` distinct mapped ``link_down`` events of that device lie within ``window_s``
seconds of each other, and the configured side holds a link in force on that device. Anything else
selects nothing, and nothing is stored for an unselected state.

The thresholds are inputs, read by the caller from the environment. Nothing about a request, a route
or an ingest can move them, and a device can only add events, never change the rule.

A promoted conflict is a stored description of a contradiction between two sides. It never writes,
ranks or marks configured state, and it never reaches eligibility or the verdict (D-64). Event times
are authored by the sending device, so ``valid_from`` is clamped to the promotion time: a record can
not claim to have begun in the future.

The module is pure: standard library, ``data_contracts`` and ``state_reconciliation`` only, no I/O and
no clock read beyond the ``now`` argument.
"""
from dataclasses import dataclass
from datetime import datetime

from data_contracts.promotion import PROMOTED_SUPPORT_MAX, RULE_REPEATED_LINK_DOWN, promoted_id
from data_contracts.syslog_events import EVENT_KINDS
from state_reconciliation.event_reconciler import (
    CONFIGURED_LINK_IN_FORCE,
    ConfiguredSide,
    conflict_text,
)

_LINK_DOWN = "link_down"


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _aware(value, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class CountedEvent:
    """What the rule needs of a stored event: never the raw line, the host or any message text.

    ``kind`` is a recognised kind or None. ``placed_at`` is the event time, else the ingest time."""

    event_id: str
    device: str
    kind: str | None
    placed_at: datetime

    def __post_init__(self):
        if self.kind is not None and self.kind not in EVENT_KINDS:
            raise ValueError("unknown event kind")
        _aware(self.placed_at, "placed_at")


@dataclass(frozen=True)
class PromotedRecord:
    """A record the rule selected, ready to be stored. ``device_b`` is None for a conflict."""

    promoted_id: str
    kind: str
    rule: str
    device_a: str
    device_b: str | None
    body: dict
    valid_from: datetime
    event_ids: tuple


def _check_thresholds(min_events, window_s) -> None:
    if not _is_int(min_events) or min_events < 1:
        raise ValueError("min_events must be a whole number of at least 1")
    if not _is_int(window_s) or window_s < 1:
        raise ValueError("window_s must be a whole number of seconds, at least 1")


def first_window(times, min_events: int, window_s: int) -> int | None:
    """The lowest index ``i`` of ``times`` (sorted ascending) where ``min_events`` times fit in
    ``window_s`` seconds, counting from ``times[i]``; None when no run of that many fits."""
    _check_thresholds(min_events, window_s)
    ordered = list(times)
    for i in range(len(ordered) - min_events + 1):
        if (ordered[i + min_events - 1] - ordered[i]).total_seconds() <= window_s:
            return i
    return None


def select_conflicts(events, *, device: str, configured: ConfiguredSide, min_events: int,
                     window_s: int, now: datetime) -> tuple[PromotedRecord, ...]:
    """The conflict to promote for ``device``, or ``()``.

    The qualifying events are those of ``device`` with kind ``link_down``, one per event id, ordered
    by (placed_at, event_id). The inputs are not changed. ``valid_from`` is the first event of the
    first qualifying window, never after ``now``; ``event_ids`` are the first
    ``PROMOTED_SUPPORT_MAX`` qualifying ids in that order."""
    _aware(now, "now")
    _check_thresholds(min_events, window_s)
    seen = {}
    for e in sorted(events, key=lambda e: (e.placed_at, e.event_id)):
        if e.device == device and e.kind == _LINK_DOWN and e.event_id not in seen:
            seen[e.event_id] = e
    qualifying = list(seen.values())
    if configured.device != device or configured.value != CONFIGURED_LINK_IN_FORCE:
        return ()
    start = first_window([e.placed_at for e in qualifying], min_events, window_s)
    if start is None:
        return ()
    return (PromotedRecord(
        promoted_id=promoted_id("conflict", RULE_REPEATED_LINK_DOWN, (device,)),
        kind="conflict", rule=RULE_REPEATED_LINK_DOWN, device_a=device, device_b=None,
        body={"device": device, "event": _LINK_DOWN, "configured": CONFIGURED_LINK_IN_FORCE,
              "text": conflict_text(_LINK_DOWN, CONFIGURED_LINK_IN_FORCE)},
        valid_from=min(qualifying[start].placed_at, now),
        event_ids=tuple(e.event_id for e in qualifying[:PROMOTED_SUPPORT_MAX])),)
