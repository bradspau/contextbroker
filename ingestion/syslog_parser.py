"""Syslog preparation: parse RFC 5424 / RFC 3164 lines into normalised events.

Message content is treated purely as data; it is never executed or interpolated.
RFC 3164 headers carry no year or timezone, so event_time is left None rather
than guessed. The original line is always retained in ``raw``.
"""
import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SyslogEvent:
    raw: str
    device_id: str | None
    severity: int | None
    event_time: datetime | None
    message: str


_PRI = re.compile(r"<(\d{1,3})>")
_3164 = re.compile(r"[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+(\S+)\s*(.*)$", re.S)


def _parse_ts(ts: str) -> datetime | None:
    if ts == "-":
        return None
    try:
        dt = datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith(("Z", "z")) else ts)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _device(host: str, known: dict[str, str]) -> str | None:
    h = host.lower()
    for k, v in known.items():
        if isinstance(k, str) and k.lower() == h:
            return v
    return None


def _skip_sd(rest: str) -> str:
    rest = rest.lstrip()
    if rest.startswith("-"):
        return rest[1:].lstrip()
    if not rest.startswith("["):
        return rest
    i, in_q = 0, False
    while i < len(rest):
        c = rest[i]
        if in_q:
            if c == "\\":
                i += 1
            elif c == '"':
                in_q = False
        elif c == '"':
            in_q = True
        elif c == "]" and not rest[i + 1:i + 2] == "[":
            return rest[i + 1:].lstrip()
        i += 1
    return ""


def parse_syslog(line: str, known_devices: dict[str, str]) -> SyslogEvent:
    if not isinstance(line, str):
        return SyslogEvent(raw=line, device_id=None, severity=None, event_time=None, message="")
    stripped = line.strip()
    m = _PRI.match(stripped)
    if not m or int(m.group(1)) > 191:
        return SyslogEvent(line, None, None, None, stripped)
    severity = int(m.group(1)) % 8
    body = stripped[m.end():]
    host = None
    event_time = None
    message = body.strip()
    if body.startswith("1 "):
        parts = body.split(None, 6)  # ver ts host app procid msgid rest
        if len(parts) >= 6:
            event_time = _parse_ts(parts[1])
            host = parts[2]
            message = _skip_sd(parts[6]) if len(parts) > 6 else ""
    else:
        m3 = _3164.match(body)
        if m3:
            host, message = m3.group(1), m3.group(2).strip()
    device_id = _device(host, known_devices) if host and host != "-" else None
    return SyslogEvent(line, device_id, severity, event_time, message)
