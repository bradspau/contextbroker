"""Promote a stored events situation's conflicts into the state file (SEM-01, D-56 to D-60, D-66).

An operator names one stored situation that holds the events class. The command reads that
situation's events from the state file and the configured links from the network graph in Fuseki
(SELECT queries only, and only when a rule could select), a fixed pure rule decides, and one
transaction writes the selected records with their valid-from and their support. A state the rule
does not select is not stored.

    python -m ingestion.promote --situation sit-... [--dry-run] [--query-endpoint URL]
    docker exec contextbroker-api python -m ingestion.promote --situation sit-... --dry-run

Nothing else writes a promoted row: no request, no ingest and no timer. A re-run on the same situation
refreshes the record instead of adding a second one. ``--dry-run`` does everything but the write.

The command prints one summary line of counts only:

    conflicts=N hypotheses=N events=N counted=N evicted=N

(prefixed ``dry run, nothing written: `` for a dry run). Every refusal is one fixed line on standard
error, ``Refused: <text>. Nothing was promoted.``, and exit status 1. No raw line, message text, host
or requester is printed, logged or stored in a record.
"""
import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from broker_engine import context_graph
from broker_engine.context_graph import MalformedTopology
from broker_engine.faults import log_fault
from broker_engine.sparql_resolver import FUSEKI_QUERY_ENDPOINT
from broker_state.process import get_store, load_settings, promotion_thresholds, reset_store
from broker_state.store import NewPromoted, StateUnavailable
from data_contracts.situation_record import SITUATION_ID_PATTERN, StoredSituation
from data_contracts.syslog_events import EVENT_KINDS, EVENT_SOURCE
from ingestion.load_ontologies import MissingCredential, fuseki_credentials
from state_reconciliation.event_reconciler import CONFIGURED_LINK_IN_FORCE, ConfiguredSide
from state_reconciliation.promotion import CountedEvent, select_conflicts

logger = logging.getLogger(__name__)

REFUSED_MALFORMED_ID = "the situation id is malformed"
REFUSED_NOT_FOUND = "situation not found"
REFUSED_NO_EVENTS = "the situation has no events class"
REFUSED_GRAPH_DOWN = "the network graph could not be reached"
REFUSED_GRAPH_MALFORMED = "the network graph answered with an unusable topology"
REFUSED_STATE = "state store unavailable"

_NOTHING = ". Nothing was promoted."
_DRY_PREFIX = "dry run, nothing written: "


class PromoteRefused(Exception):
    """A refusal of one run. ``text`` is one of the fixed ``REFUSED_*`` strings, never server text."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


@dataclass(frozen=True)
class PromoteSummary:
    """What one run did, as counts only."""

    events: int
    counted: int
    conflicts: int
    hypotheses: int
    evicted: int
    dry_run: bool


def summary_line(summary: PromoteSummary) -> str:
    line = (f"conflicts={summary.conflicts} hypotheses={summary.hypotheses} "
            f"events={summary.events} counted={summary.counted} evicted={summary.evicted}")
    return _DRY_PREFIX + line if summary.dry_run else line


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _instant(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def _read_situation(store, situation_id: str, now: datetime) -> StoredSituation:
    row = store.get_situation(situation_id, now=now)
    if row is None:
        raise PromoteRefused(REFUSED_NOT_FOUND)
    try:
        stored = StoredSituation.model_validate_json(row.body)
    except ValueError:  # a body that does not read back is not a usable situation
        raise PromoteRefused(REFUSED_NOT_FOUND) from None
    if "events" not in stored.situation.classes:
        raise PromoteRefused(REFUSED_NO_EVENTS)
    return stored


def promote_situation(situation_id: str, *, store, query_endpoint, min_events: int, window_s: int,
                      now: datetime, dry_run: bool = False) -> PromoteSummary:
    """Select and (unless ``dry_run``) store the promoted records of one stored events situation.

    Raises ``PromoteRefused`` with a fixed text for every refusal; nothing is written then. The
    write is one ``store.promote`` call, so a run is all or nothing."""
    if not SITUATION_ID_PATTERN.match(situation_id):
        raise PromoteRefused(REFUSED_MALFORMED_ID)
    try:
        stored = _read_situation(store, situation_id, now)
        device = stored.situation.scope.device
        ids = sorted({p.reference for p in stored.evidence.provenance
                      if p.source_system == EVENT_SOURCE})
        rows = store.events_by_ids(ids, now=now) if ids else []
        # Mapped events of the situation's device only; an ambiguous or unmapped event has no
        # device to count. The kind may be None: plan 07-07 reads the unrecognised ones too.
        counted = [CountedEvent(r.event_id, r.device, r.kind, r.event_time or r.ingest_time)
                   for r in rows if r.mapping == "mapped" and r.device == device
                   and (r.kind is None or r.kind in EVENT_KINDS)]
        configured = ConfiguredSide(device, None)
        if sum(1 for e in counted if e.kind == "link_down") >= min_events:
            try:
                links = context_graph.device_links_in_force(
                    device, valid_at=_instant(stored.situation.valid_at),
                    known_at=_instant(stored.situation.known_at), retrieved_at=now,
                    allowed=lambda _device: True, query_endpoint=query_endpoint)
            except requests.RequestException:
                raise PromoteRefused(REFUSED_GRAPH_DOWN) from None
            except MalformedTopology:
                raise PromoteRefused(REFUSED_GRAPH_MALFORMED) from None
            if links.status == "held":
                configured = ConfiguredSide(device, CONFIGURED_LINK_IN_FORCE)
        records = select_conflicts(counted, device=device, configured=configured,
                                   min_events=min_events, window_s=window_s, now=now)
        evicted = 0
        if records and not dry_run:
            batch = store.promote(
                [NewPromoted(promoted_id=r.promoted_id, kind=r.kind, rule=r.rule,
                             device_a=r.device_a, device_b=r.device_b,
                             body=json.dumps(r.body, sort_keys=True), valid_from=r.valid_from,
                             situation_id=situation_id,
                             situation_requester=stored.situation.requester, event_ids=r.event_ids)
                 for r in records], now=now)
            evicted = batch.evicted
    except StateUnavailable as exc:
        log_fault(logger, exc, "promotion could not use the state store")
        raise PromoteRefused(REFUSED_STATE) from None
    return PromoteSummary(events=len(rows), counted=len(counted), conflicts=len(records),
                          hypotheses=0, evicted=evicted, dry_run=dry_run)


def _refuse(text: str) -> int:
    print(f"Refused: {text}{_NOTHING}", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--situation", required=True, metavar="ID",
                    help="the stored situation (sit- and 32 hex digits) that holds the events class")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print what would be written; write nothing")
    ap.add_argument("--query-endpoint", default=FUSEKI_QUERY_ENDPOINT,
                    help="Fuseki query endpoint (default FUSEKI_QUERY_ENDPOINT)")
    args = ap.parse_args(argv)
    try:
        fuseki_credentials()  # before any network call
    except MissingCredential as exc:
        return _refuse(str(exc).rstrip("."))
    try:
        load_settings()
        min_events, window_s = promotion_thresholds()
    except ValueError as exc:  # the text names a variable, never a value
        return _refuse(str(exc).rstrip("."))
    if not SITUATION_ID_PATTERN.match(args.situation):
        return _refuse(REFUSED_MALFORMED_ID)
    try:
        try:
            store = get_store()
            summary = promote_situation(
                args.situation, store=store, query_endpoint=args.query_endpoint,
                min_events=min_events, window_s=window_s, now=_utcnow(), dry_run=args.dry_run)
        except StateUnavailable as exc:
            log_fault(logger, exc, "promotion could not open the state store")
            return _refuse(REFUSED_STATE)
        except PromoteRefused as exc:
            return _refuse(exc.text)
    finally:
        reset_store()
    print(summary_line(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
