"""Syslog preparation: read one RFC 5424 or RFC 3164 line, or refuse it.

`parse_line` returns a `ParsedLine` or a `Quarantined` value; it never raises on
input and never returns a half-read event (D-20). Host mapping is a separate step,
`map_host`, which keeps ambiguity instead of guessing (D-09).

Message content is treated purely as data. RFC 3164 headers carry no year or
timezone, so their event time is None and never guessed (D-14). See
`data_contracts.syslog_events` for the vocabulary and the rules.
"""
import re
from datetime import datetime
from typing import Any, Iterable

from data_contracts.syslog_events import MAX_LINE_BYTES, HostMapping, ParsedLine, Quarantined

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# PRI: "<" 1 to 3 ASCII digits ">" with no leading zero except the single digit 0.
_PRI = re.compile(r"<(0|[1-9][0-9]{0,2})>")

# RFC 3339 as RFC 5424 uses it: upper-case T and Z, a 1 to 6 digit fraction, a
# numeric offset or Z. Matched BEFORE datetime.fromisoformat, which on Python 3.14
# also accepts date-only, basic-format and lower-case forms.
# The ranges are in the pattern too, because Python 3.14 reads hour 24 as midnight; the
# calendar (Feb 30) is left to fromisoformat. Second 60 (a leap second) is refused.
_TIMESTAMP = (
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)

# RFC 5424 SD-ID: 1 to 32 printable US-ASCII except "=", space, "]" and a double quote.
_SD_NAME = r'[!#-<>-\\^-~]{1,32}'
_SD_PARAM = _SD_NAME + r'="(?:[^"\\\]]|\\.)*"'
_SD_ELEMENT = r"\[" + _SD_NAME + r"(?: " + _SD_PARAM + r")*\]"

_5424 = re.compile(
    r"1 (?P<ts>-|" + _TIMESTAMP + r")"
    r" (?P<host>-|[!-~]{1,255})"
    r" (?P<app>-|[!-~]{1,48})"
    r" (?P<procid>-|[!-~]{1,128})"
    r" (?P<msgid>-|[!-~]{1,32})"
    r" (?:-|(?:" + _SD_ELEMENT + r")+)"
    r"(?: (?P<msg>.*))?\Z",
    re.S,
)

_3164 = re.compile(
    r"(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r" (?P<day> [1-9]|0[1-9]|[12][0-9]|3[01])"
    r" (?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r" (?P<host>[!-~]{1,255})"
    r"(?: (?P<msg>.*))?\Z",
    re.S,
)

_BOM = "\ufeff"  # U+FEFF, the decoded UTF-8 byte order mark

# C0 controls except tab, DEL and the C1 controls: not text, whatever the encoding.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")


def _event_time(text: str) -> datetime | None:
    """An aware datetime from text already matched by `_TIMESTAMP`, or None for "-"."""
    if text == "-":
        return None
    dt = datetime.fromisoformat(text)  # ValueError for a month 13, hour 24, second 60 ...
    if dt.tzinfo is None:  # cannot happen after the regex; kept as a guard
        raise ValueError("no zone")
    return dt


def _day_exists(month: str, day: int) -> bool:
    # Leap year 2000 only so that "Feb 29" is allowed; no year is ever recorded.
    try:
        datetime(2000, _MONTHS.index(month) + 1, day)
    except ValueError:
        return False
    return True


def _body(pri_end: int, text: str) -> tuple[str, str | None, datetime | None, str] | None:
    """(format, host, event_time, message) for the text after the PRI, or None."""
    body = text[pri_end:]
    if body.startswith("1 "):
        m = _5424.match(body)
        if m is None:
            return None
        try:
            when = _event_time(m.group("ts"))
        except ValueError:
            return None
        message = m.group("msg") or ""
        if message.startswith(_BOM):
            message = message[1:]
        return "rfc5424", m.group("host"), when, message
    m = _3164.match(body)
    if m is None or not _day_exists(m.group("mon"), int(m.group("day"))):
        return None
    return "rfc3164", m.group("host"), None, m.group("msg") or ""


def _read_text(raw: Any) -> "str | Quarantined":
    """The decoded line, or the first of: not text (type), too long, not text (content)."""
    if isinstance(raw, str):
        # Every UTF-8 byte is at most one character wide, so more characters than the cap
        # is over the cap whatever the encoding; nothing longer is ever encoded or scanned.
        if len(raw) > MAX_LINE_BYTES:
            return Quarantined("too long")
        data = raw.encode("utf-8", "surrogatepass")  # a lone surrogate fails the decode below
    elif isinstance(raw, bytes):
        data = raw
    else:
        return Quarantined("not text")
    if len(data) > MAX_LINE_BYTES:
        return Quarantined("too long")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return Quarantined("not text")
    if _CONTROL.search(text):
        return Quarantined("not text")
    return text


def parse_line(raw: "bytes | str") -> "ParsedLine | Quarantined":
    """Read one syslog line (terminator already removed) or quarantine it.

    The checks run in one pinned order, so a line has exactly one reason: input type,
    length in bytes (over MAX_LINE_BYTES is "too long", before any decoding), strict
    UTF-8 and control characters ("not text"), a valid PRI at the first byte
    ("no priority"), then a header that fits RFC 5424 or RFC 3164 ("unparsable header").
    A readable header whose host maps to no device is still a ParsedLine (D-20).

    RFC 3164 says a receiver with no valid PRI treats the packet as content with
    priority 13. This parser deliberately quarantines such a line as "no priority"
    instead (D-20). The reason is all that is kept; the line is never stored in the
    result, an exception or a log line (D-21).
    """
    text = _read_text(raw)
    if isinstance(text, Quarantined):
        return text
    pri = _PRI.match(text)
    if pri is None or int(pri.group(1)) > 191:
        return Quarantined("no priority")
    read = _body(pri.end(), text)
    if read is None:
        return Quarantined("unparsable header")
    fmt, host, when, message = read
    value = int(pri.group(1))
    return ParsedLine(fmt, value // 8, value % 8, when, host, message, text)


def map_host(host: str, devices: Iterable[str]) -> HostMapping:
    """Map a source host, as written, to a device id; keep ambiguity (D-07, D-09).

    Exact case first, then a case-insensitive comparison. Two or more device ids that
    fold to the host make it ambiguous. Nothing is stripped (a domain, a prefix), no
    address is resolved, and the RFC 5424 nil host "-" is never mapped.
    """
    unmapped = HostMapping("unmapped", None, ())
    if host == "-":
        return unmapped
    known = {d for d in devices if isinstance(d, str)}
    if host in known:
        return HostMapping("mapped", host, (host,))
    folded = host.lower()
    matches = sorted(d for d in known if d.lower() == folded)
    if len(matches) == 1:
        return HostMapping("mapped", matches[0], (matches[0],))
    if matches:
        return HostMapping("ambiguous", None, tuple(matches))
    return unmapped
