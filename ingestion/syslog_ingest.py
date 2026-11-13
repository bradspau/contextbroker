"""Store syslog lines as events in the broker's state file.

Reads syslog lines as bytes from a file or from standard input, parses each one strictly
(RFC 5424 or RFC 3164), maps its source host to a device id and stores it. Nothing listens on
a port and no route is added: this is a command, run by an operator.

    python -m ingestion.syslog_ingest [--file PATH] [--query-endpoint URL]
    docker exec -i contextbroker-api python -m ingestion.syslog_ingest < syslog.txt

The device table is read from the network graph in Fuseki (the distinct device names of its
interfaces) once, before any line is read, and is part of each stored record: a stored event
keeps the mapping it got at ingest time, and a later change of the table does not remap it.
If the network graph cannot be read, nothing is stored and no counter changes.

A line that cannot be read is not stored. It is counted under one of four reasons (no priority,
not text, too long, unparsable header) and its text is never kept, printed or logged. The same
line sent twice is stored once and counted as a duplicate. The command prints one summary line:

    stored=N duplicates=N unmapped=N ambiguous=N quarantined=N (no priority=N, not text=N, ...)

Every refusal is one fixed line on standard error and exit status 1. Lines are written in
batches of at most 200 per transaction, so the API's own writes are never held up for long.
"""
import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from broker_engine.context_graph import MalformedTopology, read_device_names
from broker_engine.sparql_resolver import FUSEKI_QUERY_ENDPOINT
from broker_state.process import get_store, load_settings, reset_store
from broker_state.store import EVENT_BATCH_MAX, NewEvent, StateUnavailable
from data_contracts.syslog_events import (
    MAX_LINE_BYTES,
    QUARANTINE_REASONS,
    Quarantined,
    event_id,
    recognise,
)
from ingestion.load_ontologies import MissingCredential, fuseki_read_credentials
from ingestion.syslog_parser import map_host, parse_line

BATCH_SIZE = 200


@dataclass(frozen=True)
class IngestSummary:
    """What one run did. ``quarantined`` has one count for each of the four reasons."""

    lines: int
    stored: int
    duplicates: int
    unmapped: int
    ambiguous: int
    quarantined: dict


def summary_line(summary: IngestSummary) -> str:
    reasons = ", ".join(f"{reason}={summary.quarantined[reason]}" for reason in QUARANTINE_REASONS)
    return (f"stored={summary.stored} duplicates={summary.duplicates} "
            f"unmapped={summary.unmapped} ambiguous={summary.ambiguous} "
            f"quarantined={sum(summary.quarantined.values())} ({reasons})")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ingest_lines(lines, *, store, devices, clock=None, batch_size=BATCH_SIZE,
                 on_batch=None) -> IngestSummary:
    """Parse, map and store ``lines`` (bytes or str) against the device table ``devices``.

    Every ``batch_size`` lines, and at the end, one ``store.add_events`` call writes the events of
    the batch and its quarantine counts in one transaction. The ingest time is one clock read per
    batch. ``on_batch`` (optional) is called with each ``EventBatch`` once it is written. Nothing
    about a line, a host or a device is logged or printed."""
    if (not isinstance(batch_size, int) or isinstance(batch_size, bool)
            or not 1 <= batch_size <= EVENT_BATCH_MAX):
        raise ValueError("batch_size must be a whole number from 1 to %d" % EVENT_BATCH_MAX)
    clock = clock or _utc_now
    devices = tuple(devices)
    totals = {"lines": 0, "stored": 0, "duplicates": 0, "unmapped": 0, "ambiguous": 0}
    quarantined_total = {reason: 0 for reason in QUARANTINE_REASONS}
    pending = []  # (ParsedLine, HostMapping) of the current batch
    counts = {reason: 0 for reason in QUARANTINE_REASONS}
    in_batch = 0

    def flush():
        nonlocal pending, counts, in_batch
        if pending or any(counts.values()):
            ingest_time = clock()
            events = [NewEvent(event_id=event_id(p.raw), ingest_time=ingest_time,
                               event_time=p.event_time, severity=p.severity, facility=p.facility,
                               format=p.format, source_host=p.host, mapping=m.status,
                               device=m.device, candidates=m.candidates,
                               kind=recognise(p.message), raw=p.raw)
                      for p, m in pending]
            batch = store.add_events(events, quarantined={r: n for r, n in counts.items() if n})
            totals["stored"] += batch.stored
            totals["duplicates"] += batch.duplicates
            totals["unmapped"] += batch.unmapped
            totals["ambiguous"] += batch.ambiguous
            for reason, n in counts.items():
                quarantined_total[reason] += n
            if on_batch is not None:
                on_batch(batch)
        pending, counts, in_batch = [], {reason: 0 for reason in QUARANTINE_REASONS}, 0

    for raw in lines:
        totals["lines"] += 1
        parsed = parse_line(raw)
        if isinstance(parsed, Quarantined):
            counts[parsed.reason] += 1
        else:
            pending.append((parsed, map_host(parsed.host, devices)))
        in_batch += 1
        if in_batch >= batch_size:
            flush()
    flush()
    return IngestSummary(quarantined=quarantined_total, **totals)


def _strip_eol(piece: bytes) -> bytes:
    piece = piece[:-1] if piece.endswith(b"\n") else piece
    return piece[:-1] if piece.endswith(b"\r") else piece


def read_lines(stream, max_bytes=MAX_LINE_BYTES):
    """Yield the lines of a binary ``stream`` as bytes, holding at most one capped line.

    Only bounded ``readline(max_bytes + 1)`` calls are made, never ``read()``. One trailing
    CR LF (or LF) is removed and empty lines are skipped. A line longer than ``max_bytes`` is
    yielded as its first ``max_bytes + 1`` bytes (enough for the parser to call it too long) and
    the rest of it is read away in bounded pieces, so the next line is still read."""
    while True:
        piece = stream.readline(max_bytes + 1)
        if not piece:
            return
        if len(piece) > max_bytes and not piece.endswith(b"\n"):
            yield piece
            while True:  # skip the rest of the long line
                rest = stream.readline(max_bytes + 1)
                if not rest or rest.endswith(b"\n"):
                    break
            continue
        line = _strip_eol(piece)
        if line:
            yield line


def _refuse(text: str) -> int:
    print(f"Refused: {text}", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=None, metavar="PATH",
                    help="read syslog lines from this file (default: standard input)")
    ap.add_argument("--query-endpoint", default=FUSEKI_QUERY_ENDPOINT,
                    help="Fuseki query endpoint (default FUSEKI_QUERY_ENDPOINT)")
    args = ap.parse_args(argv)
    nothing = ". Nothing was stored."
    try:
        fuseki_read_credentials()  # before any network call
    except MissingCredential as exc:
        return _refuse(f"{exc}{nothing}")
    try:
        load_settings()
    except ValueError as exc:  # the text names a variable, never a value
        return _refuse(f"{exc}{nothing}")
    try:
        devices = read_device_names(args.query_endpoint)
    except requests.RequestException:
        return _refuse(f"the network graph could not be reached{nothing}")
    except MalformedTopology:
        return _refuse(f"the network graph answered with an unusable device list{nothing}")
    if not devices:
        print("Warning: the network graph lists no device, so every event is unmapped.",
              file=sys.stderr)
    stored = [0]

    def count(batch):
        stored[0] += batch.stored

    def stopped(what: str) -> int:
        if stored[0]:
            return _refuse(f"{what} after {stored[0]} events were stored.")
        return _refuse(f"{what}{nothing}")

    stream = None
    try:
        try:
            stream = open(args.file, "rb") if args.file is not None else sys.stdin.buffer
        except OSError:
            return _refuse(f"the input file could not be read{nothing}")
        try:
            store = get_store()
            summary = ingest_lines(read_lines(stream), store=store, devices=devices,
                                   batch_size=BATCH_SIZE, on_batch=count)
        except StateUnavailable:
            return stopped("state store unavailable")
        except OSError:
            return stopped("the input file could not be read")
    finally:
        if stream is not None and args.file is not None:
            stream.close()
        reset_store()
    print(summary_line(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
