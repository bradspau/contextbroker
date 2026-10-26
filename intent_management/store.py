"""Intent management read model: ownership boundary for intents (broker does not own them)."""
import threading
from dataclasses import dataclass
from typing import Callable

from outcome_assurance.evaluate import Outcome

_REQUIRED = ("metric", "operator", "target_value", "interfaces")


@dataclass(frozen=True)
class Intent:
    id: str
    metric: str | None
    operator: str | None
    target_value: float | None
    interfaces: list[dict]
    status: str
    last_outcome: Outcome | None = None

    def as_resolver_dict(self) -> dict:
        return {"metric": self.metric, "operator": self.operator,
                "target_value": self.target_value, "interfaces": self.interfaces}


class IntentStore:
    def __init__(self, resolver: Callable[[str], dict], _outcomes=None, _lock=None):
        self._resolver = resolver
        self._outcomes: dict[str, Outcome] = {} if _outcomes is None else _outcomes
        self._lock = _lock or threading.Lock()

    def with_resolver(self, resolver: Callable[[str], dict]) -> "IntentStore":
        """New store using `resolver` but sharing this store's fulfilment record."""
        return IntentStore(resolver, self._outcomes, self._lock)

    def get(self, intent_id: str) -> Intent:
        data = self._resolver(intent_id)
        with self._lock:
            last = self._outcomes.get(intent_id)
        status = "active" if all(k in data for k in _REQUIRED) else "unknown"
        return Intent(
            id=intent_id, metric=data.get("metric"), operator=data.get("operator"),
            target_value=data.get("target_value"),
            interfaces=data.get("interfaces") or [], status=status, last_outcome=last,
        )

    def fulfilment_update(self, intent_id: str, outcome: Outcome) -> None:
        with self._lock:
            self._outcomes[intent_id] = outcome
