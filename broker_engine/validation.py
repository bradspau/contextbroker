"""Input validation shared by the broker engine query builders."""
import re

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_\-/]+$")
_SAFE_WINDOW = re.compile(r"^[0-9]+[smhd]$")


def _validate_token(value: str, label: str) -> str:
    if not _SAFE_TOKEN.match(value):
        raise ValueError(f"Unsafe {label!r}: {value!r}")
    return value
