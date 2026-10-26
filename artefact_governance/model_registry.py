"""Registry of model artefacts and their lifecycle status."""

_STATUSES = {"candidate", "approved", "retired"}


class ModelRegistry:
    def __init__(self):
        self._status: dict[tuple[str, str], str] = {}

    def register(self, name: str, version: str, status: str) -> None:
        if status not in _STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        self._status[(name, version)] = status

    def status(self, name: str, version: str) -> str | None:
        return self._status.get((name, version))
