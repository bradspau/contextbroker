"""System 1 event reconciler (stub): syslog events -> semantic state.

Event-derived state is always an OBSERVED envelope and is never presented as
CONFIGURED. If the current envelope is CONFIGURED (authoritative) and the event
message differs from its value, the contradiction is published in
``ReconcileResult.conflicts`` (and ``envelope.quality.conflicts``) rather than
overwriting anything. ``reconcile`` never mutates or returns a modified
``current``; callers decide what to do with the observed fact and conflicts.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from data_contracts.envelope import (
    DEFAULT_MAX_AGE_S,
    ContextEnvelope,
    StateCategory,
    build_observed,
)
from ingestion.syslog_parser import SyslogEvent


@dataclass(frozen=True)
class ReconcileResult:
    envelope: ContextEnvelope | None
    conflicts: tuple[str, ...]


def _norm(v) -> str:
    return str(v).strip().lower()


def reconcile(
    event: SyslogEvent,
    current: ContextEnvelope | None,
    now: datetime | None = None,
    max_age_s: int = DEFAULT_MAX_AGE_S,
) -> ReconcileResult:
    if event.device_id is None:
        return ReconcileResult(None, ("unresolved device",))
    now = now or datetime.now(timezone.utc)
    env = build_observed(event.device_id, event.message, "syslog", event.event_time, now, max_age_s)
    if event.event_time is None:
        env.quality.missing = True
    env.provenance = {
        "raw": event.raw,
        "severity": event.severity,
        "reconciler": "system1-event-reconciler-stub",
        "reconciler_version": "0.1",
    }
    conflicts: tuple[str, ...] = ()
    if (
        current is not None
        and current.state is StateCategory.CONFIGURED
        and _norm(current.value) != _norm(event.message)
    ):
        conflicts = (f"event contradicts configured state for {event.device_id}",)
        env.quality.conflicts = list(conflicts)
    return ReconcileResult(env, conflicts)
