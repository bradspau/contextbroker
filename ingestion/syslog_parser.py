"""Syslog preparation: read one RFC 5424 or RFC 3164 line, or refuse it.

`parse_line` returns a `ParsedLine` or a `Quarantined` value; it never raises on
input and never returns a half-read event (D-20). Host mapping is a separate step,
`map_host`, which keeps ambiguity instead of guessing (D-09).

Message content is treated purely as data. RFC 3164 headers carry no year or
timezone, so their event time is None and never guessed (D-14). See
`data_contracts.syslog_events` for the vocabulary and the rules.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from data_contracts.syslog_events import HostMapping, ParsedLine, Quarantined

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class SyslogEvent:
    """Normalised event. ``raw`` is the original input; non-str input is preserved as-is."""

    # Remains for the reconciler until plan 05-05 rewrites it and removes this class.
    raw: "str | Any"
    device_id: str | None
    severity: int | None
    event_time: datetime | None
    message: str


# PRI: "<" 1 to 3 ASCII digits ">" with no leading zero except the single digit 0.
_PRI = re.compile(r"<(0|[1-9][0-9]{0,2})>")

# RFC 3339 as RFC 5424 uses it: upper-case T and Z, a 1 to 6 digit fraction, a
# numeric offset or Z. Matched BEFORE datetime.fromisoformat, which on Python 3.14
# also accepts date-only, basic-format and lower-case forms.
_TIMESTAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"

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

_BOM = "﻿"


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


def parse_line(raw: "bytes | str") -> "ParsedLine | Quarantined":
    """Read one syslog line (terminator already removed) or quarantine it."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return Quarantined("unparsable header")
    elif isinstance(raw, str):
        text = raw
    else:
        return Quarantined("unparsable header")
    pri = _PRI.match(text)
    if pri is None or int(pri.group(1)) > 191:
        return Quarantined("unparsable header")
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
