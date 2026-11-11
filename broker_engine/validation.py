"""Input validation shared by the broker engine query builders."""
import os
import re

from data_contracts.envelope import DEFAULT_MAX_AGE_S

REQUEST_TIMEOUT_S = 10


class ClientInputError(ValueError):
    """Caller-supplied input failed validation (maps to HTTP 400)."""


SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_\-/]+\Z")
SAFE_WINDOW = re.compile(r"^[0-9]+[smhd]\Z")


def validate_token(value: str, label: str) -> str:
    if not SAFE_TOKEN.match(value):
        raise ClientInputError(f"Unsafe {label!r}: {value!r}")
    return value


UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# One operator value feeds VictoriaMetrics' -retentionPeriod flag and the API's as_of limit
# (docker-compose.yml). Only digits then h or d: VictoriaMetrics reads a bare number as months.
RETENTION_ENV = "BROKER_RETENTION_PERIOD"
_DEFAULT_RETENTION = "30d"
SAFE_RETENTION = re.compile(r"^[0-9]+[hd]\Z")


def max_as_of_seconds(environ=os.environ) -> int:
    """The furthest back an as_of (or known_at) may reach: the VictoriaMetrics retention.

    Read from BROKER_RETENTION_PERIOD (default 30d). ValueError, naming the variable but
    never its value, if it is not digits then h or d or is under one day (the VictoriaMetrics
    minimum)."""
    value = environ.get(RETENTION_ENV) or _DEFAULT_RETENTION
    if not SAFE_RETENTION.match(value):
        seconds = 0
    else:
        seconds = int(value[:-1]) * UNIT_SECONDS[value[-1]]
    if seconds < 86400:
        raise ValueError(f"{RETENTION_ENV} must be digits then h or d, at least 1d, for example 30d")
    return seconds


# How many per-metric telemetry reads one assess may run at once (broker_engine/telemetry_reads.py).
TELEMETRY_WORKERS_ENV = "BROKER_TELEMETRY_WORKERS"
DEFAULT_TELEMETRY_WORKERS = 8
MAX_TELEMETRY_WORKERS = 8
_DIGITS = re.compile(r"^[0-9]+\Z")


def telemetry_workers(environ=os.environ) -> int:
    """The cap on concurrent telemetry reads per request: BROKER_TELEMETRY_WORKERS, a whole
    number from 1 to 8 (default 8; blank means the default). 1 reads the metrics one by one.
    ValueError, naming the variable but never its value, for anything else."""
    raw = environ.get(TELEMETRY_WORKERS_ENV)
    if not raw:
        return DEFAULT_TELEMETRY_WORKERS
    if _DIGITS.match(raw) and 1 <= int(raw) <= MAX_TELEMETRY_WORKERS:
        return int(raw)
    raise ValueError(f"{TELEMETRY_WORKERS_ENV} must be a whole number from 1 to "
                     f"{MAX_TELEMETRY_WORKERS}")


# How many series one interfaces-nonzero rollup may read from VictoriaMetrics (operations.py).
TSDB_SERIES_LIMIT_ENV = "BROKER_TSDB_SERIES_LIMIT"
DEFAULT_TSDB_SERIES_LIMIT = 500
MAX_TSDB_SERIES_LIMIT = 100000


def tsdb_series_limit(environ=os.environ) -> int:
    """The cap on series one interfaces-nonzero query reads: BROKER_TSDB_SERIES_LIMIT, a whole
    number from 1 to 100000 (default 500; blank means the default). ValueError, naming the
    variable but never its value, for anything else."""
    raw = environ.get(TSDB_SERIES_LIMIT_ENV)
    if not raw:
        return DEFAULT_TSDB_SERIES_LIMIT
    if _DIGITS.match(raw) and 1 <= int(raw) <= MAX_TSDB_SERIES_LIMIT:
        return int(raw)
    raise ValueError(f"{TSDB_SERIES_LIMIT_ENV} must be a whole number from 1 to "
                     f"{MAX_TSDB_SERIES_LIMIT}")


def window_seconds(value, label: str) -> int:
    """Seconds in a "5m"/"1h" style duration; ClientInputError if malformed or further
    back than the retention period (max_as_of_seconds). Used for as_of, an offset back from
    now. `label` is a literal from code, never caller text."""
    if not isinstance(value, str) or not SAFE_WINDOW.match(value):
        raise ClientInputError(f"Unsafe {label}: {value!r}")
    seconds = int(value[:-1]) * UNIT_SECONDS[value[-1]]
    if seconds > max_as_of_seconds():
        raise ClientInputError(f"{label} is further back than the retention period")
    return seconds


def freshness_seconds(value, label: str = "freshness") -> int:
    """Seconds in a "2m"/"300s" style freshness limit; ClientInputError if malformed or outside
    1 s to DEFAULT_MAX_AGE_S. A freshness expectation can only tighten the default limit (D-13), so a
    larger value is refused, not clamped. Not window_seconds: its retention cap means something else.
    The messages are fixed and never echo `value`. `label` is a literal from code, never caller text."""
    if not isinstance(value, str) or not SAFE_WINDOW.match(value):
        raise ClientInputError(f"{label} must be a duration of 1 s to {DEFAULT_MAX_AGE_S} s using s, m, h or d")
    seconds = int(value[:-1]) * UNIT_SECONDS[value[-1]]
    if not 1 <= seconds <= DEFAULT_MAX_AGE_S:
        raise ClientInputError(f"{label} must be from 1 s to {DEFAULT_MAX_AGE_S} s")
    return seconds
