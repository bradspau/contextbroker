"""Promote a stored events situation's conflicts and hypotheses into the state file (SEM-01, D-56 to D-66).

An operator names one stored situation that holds the events class. The command reads that
situation's events from the state file and the configured links from the network graph in Fuseki
(SELECT queries only, and only when a rule could select), a fixed pure rule decides, and one
transaction writes the selected records with their valid-from and their support. A state the rule
does not select is not stored.

    python -m ingestion.promote --situation sit-... [--dry-run] [--query-endpoint URL]
    docker exec contextbroker-api python -m ingestion.promote --situation sit-... --dry-run

Nothing else writes a promoted row: no request, no ingest and no timer. A re-run on the same situation
refreshes the record instead of adding a second one. ``--dry-run`` does everything but the write.

A hypothesis is inferred only from neighbour-discovery lines (``data_contracts.adjacency``), read from
the stored raw text of the situation's events at this moment and nowhere else. The neighbour and
interface names a line carries are used as lookup keys against the graph's own names; only graph names
are stored, and no line text, neighbour text or host is stored, printed or logged.

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
from data_contracts.adjacency import recognise_adjacency
from data_contracts.situation_record import SITUATION_ID_PATTERN, StoredSituation
from data_contracts.syslog_events import EVENT_KINDS, EVENT_SOURCE
from data_contracts.syslog_events import Quarantined
from ingestion.load_ontologies import MissingCredential, fuseki_read_credentials
from ingestion.syslog_parser import map_host, parse_line
from state_reconciliation.event_reconciler import CONFIGURED_LINK_IN_FORCE, ConfiguredSide
from state_reconciliation.promotion import (
    AdjacencyObservation,
    CountedEvent,
    select_conflicts,
    select_hypotheses,
)

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


def _placed_at(row) -> datetime:
    """The time a rule counts an event at: the time the device wrote, never later than the time the broker
    received it. Syslog origin is unauthenticated, so a future-dated line would otherwise sit in any
    window; clamped to the ingest time it counts as received. An event with no time counts at ingest."""
    if row.event_time is None:
        return row.ingest_time
    return min(row.event_time, row.ingest_time)


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


def _hypotheses(counted, stored, *, store, query_endpoint, min_events, window_s, now):
    """The hypothesis records of the counted events, or ``()``.

    Raw text is read only here, and nothing is read from Fuseki unless some stored line is a
    neighbour-discovery line. A written neighbour is a lookup key for ``map_host``; an interface
    name must equal a graph interface of the device exactly. Only graph names reach a record. A
    conflicting set of link records gives no hypothesis; a Fuseki failure or an unusable answer
    refuses the run."""
    found = []
    for e in counted:
        raw = store.event_raw(e.event_id)
        parsed = None if raw is None else parse_line(raw)
        if parsed is None or isinstance(parsed, Quarantined):
            continue
        adjacency = recognise_adjacency(parsed.message)
        if adjacency is not None:
            found.append((e, adjacency))
    if not found:
        return ()
    try:
        names = context_graph.read_device_names(query_endpoint)
        peers = {}
        for _e, a in found:
            if a.peer not in peers:
                mapping = map_host(a.peer, names)
                peers[a.peer] = mapping.device if mapping.status == "mapped" else None
        mapped = {device for device in peers.values() if device is not None}
        if not mapped:
            return ()
        interfaces = context_graph.read_device_interfaces(
            {found[0][0].device, *mapped}, query_endpoint)
        known = frozenset(f"{d}/{n}" for _iri, d, n in interfaces)
        observations = []
        for e, a in found:
            peer = peers[a.peer]
            local_node, peer_node = f"{e.device}/{a.local_interface}", f"{peer}/{a.peer_interface}"
            if peer is not None and local_node in known and peer_node in known:
                observations.append(AdjacencyObservation(e.event_id, local_node, peer_node,
                                                         e.placed_at))
        if not observations:
            return ()
        selection = context_graph.select_links(
            context_graph.read_links([iri for iri, _d, _n in interfaces], query_endpoint),
            _instant(stored.situation.valid_at), _instant(stored.situation.known_at))
    except requests.RequestException:
        raise PromoteRefused(REFUSED_GRAPH_DOWN) from None
    except MalformedTopology:
        raise PromoteRefused(REFUSED_GRAPH_MALFORMED) from None
    if selection.conflicting:
        return ()
    linked = frozenset(end.node_id for link in selection.links for end in link.ends)
    return select_hypotheses(observations, known_interfaces=known, linked_interfaces=linked,
                             min_events=min_events, window_s=window_s, now=now)


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
        counted = [CountedEvent(r.event_id, r.device, r.kind, _placed_at(r))
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
            if links.status == "invalid":  # conflicting or malformed link records
                raise PromoteRefused(REFUSED_GRAPH_MALFORMED)
            if links.status == "held":
                configured = ConfiguredSide(device, CONFIGURED_LINK_IN_FORCE)
        records = select_conflicts(counted, device=device, configured=configured,
                                   min_events=min_events, window_s=window_s, now=now)
        records = records + _hypotheses(counted, stored, store=store, query_endpoint=query_endpoint,
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
    return PromoteSummary(
        events=len(rows), counted=len(counted),
        conflicts=sum(1 for r in records if r.kind == "conflict"),
        hypotheses=sum(1 for r in records if r.kind == "hypothesis"),
        evicted=evicted, dry_run=dry_run)


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
        fuseki_read_credentials()  # before any network call
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
