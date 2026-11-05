"""Event reconciler: a recognised syslog event meets the configured side, and a contradiction is
published as a conflict (EVT-04).

The reconciler's inputs map onto the unchanged ``StateCategory`` (D-01): a desired state is
``intended``, a link fluent read from the graph is ``configured``, and a stored event or a controller
report is ``observed``. Nothing here adds a category.

- An event only ever produces an ``OBSERVED`` envelope; no inferred state is built from it (D-02).
- Only recognised kinds are compared, through ``CONTRADICTIONS`` (D-16). A kind outside
  ``EVENT_KINDS`` is a programming error, and an event the broker could not map or could not tell
  apart never reaches this module.
- Both sides are shown and nothing is ranked: the configured value is never written, marked stale or
  replaced, and the broker does not say which side is right (D-18, D-19).
- The configured side is that a link fluent with an end on the event's device is in force, so the
  comparison is device-level, and the conflict text does not claim the event concerns the same
  interface (D-30).

``reconcile`` is pure: no I/O and no clock read beyond its argument. Conflict text and envelope
values come only from the closed kind and configured vocabularies, never from a message.
"""
from dataclasses import dataclass
from datetime import datetime

from data_contracts.envelope import (
    DEFAULT_MAX_AGE_S,
    ContextEnvelope,
    ProvenanceRecord,
    build_observed,
)
from data_contracts.syslog_events import EVENT_KINDS, EVENT_SOURCE

CONFIGURED_LINK_IN_FORCE = "link_in_force"
CONFIGURED_VALUES = (CONFIGURED_LINK_IN_FORCE,)

# (event kind, configured value) pairs that contradict each other. Everything else, including
# link_up against a link in force, is not a conflict.
CONTRADICTIONS = frozenset({("link_down", CONFIGURED_LINK_IN_FORCE)})


@dataclass(frozen=True)
class EventFact:
    """What the reconciler needs of a stored event: never the raw line or any message text."""

    event_id: str
    device: str
    kind: str
    event_time: datetime | None


@dataclass(frozen=True)
class ConfiguredSide:
    """The configured state read for a device: one of ``CONFIGURED_VALUES``, or None when none is held."""

    device: str
    value: str | None

    def __post_init__(self):
        if self.value is not None and self.value not in CONFIGURED_VALUES:
            raise ValueError("unknown configured value")


@dataclass(frozen=True)
class ReconcileResult:
    envelope: ContextEnvelope
    conflicts: tuple[str, ...]


def conflict_text(kind: str, value: str) -> str:
    """The one fixed sentence for a conflict, built from the two closed vocabularies."""
    if kind not in EVENT_KINDS:
        raise ValueError("unknown event kind")
    if value not in CONFIGURED_VALUES:
        raise ValueError("unknown configured value")
    return f"event {kind} contradicts configured {value}"


def reconcile(event: EventFact, configured: ConfiguredSide | None, *, now: datetime,
              max_age_s: int = DEFAULT_MAX_AGE_S) -> ReconcileResult:
    """The observed envelope of one recognised event and any conflict with the configured side.

    A configured side for another device, or one with no value, gives no conflict. The inputs are
    never changed."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if event.kind not in EVENT_KINDS:
        raise ValueError("unknown event kind")
    envelope = build_observed(event.device, event.kind, EVENT_SOURCE, event.event_time, now, max_age_s)
    envelope.provenance = [ProvenanceRecord(EVENT_SOURCE, event.event_id, event.event_time, now)]
    conflicts: tuple[str, ...] = ()
    if (configured is not None and configured.device == event.device and configured.value is not None
            and (event.kind, configured.value) in CONTRADICTIONS):
        conflicts = (conflict_text(event.kind, configured.value),)
        envelope.quality.conflicts = list(conflicts)
    return ReconcileResult(envelope, conflicts)
