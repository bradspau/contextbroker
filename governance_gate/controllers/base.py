"""Controller interface: the only thing allowed to change the network."""
from typing import Protocol, runtime_checkable


@runtime_checkable
class Controller(Protocol):
    def apply(self, template_id: str, scope: str, idempotency_key: str) -> str: ...

    def rollback(self, handle: str) -> None: ...

    def compensate(self, template_id: str, scope: str, idempotency_key: str) -> None:
        """Best-effort undo of a possibly partially applied change (apply raised, no handle)."""
        ...
