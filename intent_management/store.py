"""Last fulfilment outcome per TMF921 intent, and the high-water marks of what the
broker pushed. The broker does not own intents.

This records the last outcome the broker reported for each intent and the
per-(intent, metric) high-water mark of every observation it pushed, in the
durable state store (STATE-01). The outcome table is capped and evicts the least
recently written intent (D-11). It holds no intent definition."""
from datetime import datetime

from broker_state.process import get_store
from broker_state.store import StateStore
from outcome_assurance.evaluate import Outcome


class IntentStore:
    """Façade over the state store. Without `state=` each call uses the process
    store at call time, so building one opens nothing and it survives store resets."""

    def __init__(self, *, state: StateStore | None = None):
        self._state = state

    def _store(self) -> StateStore:
        return self._state if self._state is not None else get_store()

    def last_outcome(self, intent_id: str) -> Outcome | None:
        """Recorded outcome for an id, or None if none has been recorded."""
        value = self._store().last_outcome(intent_id)
        return Outcome(value) if value is not None else None

    def tracked_count(self) -> int:
        return self._store().outcome_count()

    def fulfilment_update(self, intent_id: str, outcome: Outcome) -> None:
        if not isinstance(outcome, Outcome):
            raise TypeError("outcome must be an Outcome")
        self._store().record_outcome(intent_id, outcome.value)

    def marks(self, intent_id: str, uris) -> dict[str, tuple[datetime, float]]:
        return self._store().marks(intent_id, uris)

    def advance_mark(self, intent_id: str, uri: str, observed_at: datetime,
                     value: float) -> bool:
        return self._store().advance_mark(intent_id, uri, observed_at, value)

    def mark_count(self) -> int:
        return self._store().mark_count()
