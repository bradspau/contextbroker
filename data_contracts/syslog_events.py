"""The syslog event vocabulary shared by the parser, the store and the request path.

This module is pure data and standard library only, so `broker_engine` and
`state_reconciliation` can use the event types without importing `ingestion`.

Rules that hold for everything built on it:

- Message content is data. It is never executed, interpolated or used to build a
  query.
- RFC 3164 carries no year and no zone, so its event time is None and is never
  guessed (D-14). A nil RFC 5424 timestamp is None as well.
- The dedup key and the event id are the hash of the raw line, whatever its source
  or time, so two real events with identical text collapse into one (D-05,
  accepted trade-off).
- A quarantined line keeps only its reason. Its text, its length and any part of
  it are dropped, because it could be hostile (D-21).
"""
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

# The same tuple as broker_state.store.QUARANTINE_REASONS (a test pins them equal).
QUARANTINE_REASONS = ("no priority", "not text", "too long", "unparsable header")

MAX_LINE_BYTES = 4096

EVENT_FORMATS = ("rfc5424", "rfc3164")
EVENT_MAPPINGS = ("mapped", "ambiguous", "unmapped")
EVENT_KINDS = ("link_down", "link_up")

EVENT_ID_PATTERN = re.compile(r"^evt-[0-9a-f]{32}\Z")


def event_id(raw: str) -> str:
    """`evt-` plus the first 32 hex digits of SHA-256 over the UTF-8 line (D-05)."""
    return "evt-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ParsedLine:
    """One readable syslog line. `host` is as written ("-" is the RFC 5424 nil value)."""

    format: str
    facility: int
    severity: int
    event_time: datetime | None
    host: str
    message: str
    raw: str


@dataclass(frozen=True)
class Quarantined:
    """A line the parser could not read. Holds one fixed reason and nothing else."""

    reason: str

    def __post_init__(self) -> None:
        if self.reason not in QUARANTINE_REASONS:
            raise ValueError("unknown quarantine reason")


@dataclass(frozen=True)
class HostMapping:
    """The outcome of mapping a source host to a device id.

    `candidates` is (device,) when mapped, the sorted ids when ambiguous and ()
    when unmapped.
    """

    status: str
    device: str | None
    candidates: tuple[str, ...]
