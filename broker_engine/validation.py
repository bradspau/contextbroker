"""Input validation shared by the broker engine query builders."""
import re

REQUEST_TIMEOUT_S = 10


class ClientInputError(ValueError):
    """Caller-supplied input failed validation (maps to HTTP 400)."""


class IntentNotFound(ValueError):
    """No such intent in the knowledge graph (maps to HTTP 404)."""


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_\-/]+\Z")
_SAFE_WINDOW = re.compile(r"^[0-9]+[smhd]\Z")


def _validate_token(value: str, label: str) -> str:
    if not _SAFE_TOKEN.match(value):
        raise ClientInputError(f"Unsafe {label!r}: {value!r}")
    return value
