"""Input validation shared by the broker engine query builders."""
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


MAX_AS_OF_S = 30 * 86400  # no further back than VictoriaMetrics' default retention
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def window_seconds(value, label: str) -> int:
    """Seconds in a "5m"/"1h" style duration; ClientInputError if malformed or
    longer than MAX_AS_OF_S. Used for as_of, an offset back from now."""
    if not isinstance(value, str) or not _SAFE_WINDOW.match(value):
        raise ClientInputError(f"Unsafe {label}: {value!r}")
    seconds = int(value[:-1]) * _UNIT_SECONDS[value[-1]]
    if seconds > MAX_AS_OF_S:
        raise ClientInputError(f"{label} is further back than 30d: {value!r}")
    return seconds
