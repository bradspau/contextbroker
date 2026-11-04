"""Input validation shared by the broker engine query builders."""
import os
import re

REQUEST_TIMEOUT_S = 10


class ClientInputError(ValueError):
    """Caller-supplied input failed validation (maps to HTTP 400)."""


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_\-/]+\Z")
_SAFE_WINDOW = re.compile(r"^[0-9]+[smhd]\Z")


def _validate_token(value: str, label: str) -> str:
    if not _SAFE_TOKEN.match(value):
        raise ClientInputError(f"Unsafe {label!r}: {value!r}")
    return value


_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# One operator value feeds VictoriaMetrics' -retentionPeriod flag and the API's as_of limit
# (docker-compose.yml). Only digits then h or d: VictoriaMetrics reads a bare number as months.
RETENTION_ENV = "BROKER_RETENTION_PERIOD"
_DEFAULT_RETENTION = "30d"
_SAFE_RETENTION = re.compile(r"^[0-9]+[hd]\Z")


def max_as_of_seconds(environ=os.environ) -> int:
    """The furthest back an as_of (or known_at) may reach: the VictoriaMetrics retention.

    Read from BROKER_RETENTION_PERIOD (default 30d). ValueError, naming the variable but
    never its value, if it is not digits then h or d or is under one day (the VictoriaMetrics
    minimum)."""
    value = environ.get(RETENTION_ENV) or _DEFAULT_RETENTION
    if not _SAFE_RETENTION.match(value):
        seconds = 0
    else:
        seconds = int(value[:-1]) * _UNIT_SECONDS[value[-1]]
    if seconds < 86400:
        raise ValueError(f"{RETENTION_ENV} must be digits then h or d, at least 1d, for example 30d")
    return seconds


def window_seconds(value, label: str) -> int:
    """Seconds in a "5m"/"1h" style duration; ClientInputError if malformed or further
    back than the retention period (max_as_of_seconds). Used for as_of, an offset back from
    now. `label` is a literal from code, never caller text."""
    if not isinstance(value, str) or not _SAFE_WINDOW.match(value):
        raise ClientInputError(f"Unsafe {label}: {value!r}")
    seconds = int(value[:-1]) * _UNIT_SECONDS[value[-1]]
    if seconds > max_as_of_seconds():
        raise ClientInputError(f"{label} is further back than the retention period")
    return seconds
