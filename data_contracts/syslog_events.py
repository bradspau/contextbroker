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

# The source system name on every event envelope and provenance record.
EVENT_SOURCE = "syslog"

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


@dataclass(frozen=True, slots=True)
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


# The recognised-kind table (D-16). The reconciler compares only these kinds; every
# other message is stored and returned but never produces a conflict. Each pattern is
# case-sensitive and anchored: the Cisco forms must start at the "%" token (not
# preceded by a letter or digit) and end the message (\Z), and the interface name is
# limited to letters, digits and "/", ".", ":", "_", "-" up to 64 characters, so
# device-authored text cannot widen a match. The vendor wordings below are
# community-sourced (research A1), not taken from a vendor guide; the table is
# deliberately small and a new form is added here only with a near-miss test.
_IFACE = r"[A-Za-z0-9/.:_-]{1,64}"
_NOT_ALNUM_BEFORE = r"(?<![A-Za-z0-9])"

_KIND_TABLE: tuple[re.Pattern[str], ...] = (
    # Cisco IOS: %LINK-3-UPDOWN: Interface Gi0/0/0, changed state to down
    re.compile(_NOT_ALNUM_BEFORE + r"%LINK-[0-7]-UPDOWN: Interface " + _IFACE + r", changed state to (?P<state>down|up)\Z"),
    # Cisco IOS: %LINEPROTO-5-UPDOWN: Line protocol on Interface Gi0/0/0, changed state to down
    re.compile(
        _NOT_ALNUM_BEFORE
        + r"%LINEPROTO-[0-7]-UPDOWN: Line protocol on Interface "
        + _IFACE
        + r", changed state to (?P<state>down|up)\Z"
    ),
    # Linux NIC drivers (e1000e, igb, ...): "eth0 NIC Link is Up 1000 Mbps Full Duplex"
    re.compile(_NOT_ALNUM_BEFORE + r"NIC Link is (?P<state>Down|Up)(?![A-Za-z0-9])"),
)

_STATE_KIND = {"down": "link_down", "up": "link_up"}


def recognise(message: str) -> str | None:
    """The event kind of a message ("link_down" or "link_up"), or None for any other text."""
    if message.startswith("\ufeff"):
        message = message[1:]
    for pattern in _KIND_TABLE:
        found = pattern.search(message)
        if found is not None:
            return _STATE_KIND[found.group("state").lower()]
    return None
