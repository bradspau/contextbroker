"""The rule that selects a reconciliation conflict for promotion (SEM-01, D-56, D-57, D-60).

The rule ``repeated_link_down_vs_configured`` in words: a conflict is selected for a device when at
least ``min_events`` distinct mapped ``link_down`` events of that device lie within ``window_s``
seconds of each other, and the configured side holds a link in force on that device. Anything else
selects nothing, and nothing is stored for an unselected state.

The rule ``adjacency_without_configured_link`` in words: a hypothesis is selected for a pair of
interfaces when at least ``min_events`` distinct events of one device lie within ``window_s`` seconds of
each other and each names that same pair, both interfaces exist in the graph, and neither has a
configured link in force. Reading the neighbour-discovery lines and the graph is the caller's work;
this module sees only graph node ids.

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

from data_contracts.promotion import (
    PROMOTED_SUPPORT_MAX,
    RULE_ADJACENCY,
    RULE_REPEATED_LINK_DOWN,
    promoted_id,
)
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


def window_support(items, start: int, window_s: int, key, ids) -> tuple:
    """The ids of the events inside the qualifying window that begins at ``items[start]``.

    ``items`` is sorted ascending by ``key``. The window holds every item whose time is within
    ``window_s`` seconds of the first one, and the ids are the first ``PROMOTED_SUPPORT_MAX`` of them in
    order, so a late burst after many older events is supported by the burst, never by the older events."""
    begin = key(items[start])
    inside = []
    for item in items[start:]:
        if (key(item) - begin).total_seconds() > window_s:
            break
        inside.append(ids(item))
    return tuple(inside[:PROMOTED_SUPPORT_MAX])


def select_conflicts(events, *, device: str, configured: ConfiguredSide, min_events: int,
                     window_s: int, now: datetime) -> tuple[PromotedRecord, ...]:
    """The conflict to promote for ``device``, or ``()``.

    The qualifying events are those of ``device`` with kind ``link_down``, one per event id, ordered
    by (placed_at, event_id). The inputs are not changed. ``valid_from`` is the first event of the
    first qualifying window, never after ``now``; ``event_ids`` are the first
    ``PROMOTED_SUPPORT_MAX`` ids of the events inside that window, in that order."""
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
        event_ids=window_support(qualifying, start, window_s, lambda e: e.placed_at,
                                 lambda e: e.event_id)),)


@dataclass(frozen=True)
class AdjacencyObservation:
    """One stored event that names a pair of interfaces, as graph node ids (``device/interface``).

    Only graph names belong here: the caller has already mapped the written neighbour and checked
    both interfaces against the graph. ``placed_at`` is the event time, else the ingest time."""

    event_id: str
    local_node: str
    peer_node: str
    placed_at: datetime

    def __post_init__(self):
        _aware(self.placed_at, "placed_at")


def _device_of(node_id: str) -> str:
    return node_id.split("/", 1)[0]


def select_hypotheses(observations, *, known_interfaces: frozenset, linked_interfaces: frozenset,
                      min_events: int, window_s: int,
                      now: datetime) -> tuple[PromotedRecord, ...]:
    """The link hypotheses to promote, one per unordered pair of interfaces, sorted by promoted id.

    A group is the observations naming the same pair (node ids sorted). It is skipped when its two
    nodes are the same, when either is not in ``known_interfaces`` or when either is in
    ``linked_interfaces`` (a configured link in force). One event id counts once. A selected group
    needs ``min_events`` of its events inside ``window_s``; ``valid_from`` is the first event of the
    first such window, never after ``now``; ``event_ids`` are the first ``PROMOTED_SUPPORT_MAX`` of the
    group's events inside that window, in (placed_at, event_id) order. The inputs are not changed."""
    _aware(now, "now")
    _check_thresholds(min_events, window_s)
    groups: dict[tuple[str, str], dict[str, AdjacencyObservation]] = {}
    for o in sorted(observations, key=lambda o: (o.placed_at, o.event_id)):
        first, second = sorted((o.local_node, o.peer_node))
        if first == second or first not in known_interfaces or second not in known_interfaces:
            continue
        if first in linked_interfaces or second in linked_interfaces:
            continue
        groups.setdefault((first, second), {}).setdefault(o.event_id, o)
    records = []
    for (first, second), seen in groups.items():
        qualifying = list(seen.values())
        start = first_window([o.placed_at for o in qualifying], min_events, window_s)
        if start is None:
            continue
        records.append(PromotedRecord(
            promoted_id=promoted_id("hypothesis", RULE_ADJACENCY, (first, second)),
            kind="hypothesis", rule=RULE_ADJACENCY, device_a=_device_of(first),
            device_b=_device_of(second), body={"from": first, "to": second},
            valid_from=min(qualifying[start].placed_at, now),
            event_ids=window_support(qualifying, start, window_s, lambda o: o.placed_at,
                                     lambda o: o.event_id)))
    return tuple(sorted(records, key=lambda r: r.promoted_id))
