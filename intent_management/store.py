"""Last fulfilment outcome per TMF921 intent. The broker does not own intents.

This records the last outcome the broker reported for each intent, in memory
(durable state is Phase 4, STATE-01). It holds no intent definition."""
import threading

from outcome_assurance.evaluate import Outcome


class IntentStore:
    def __init__(self):
        self._outcomes: dict[str, Outcome] = {}
        self._lock = threading.Lock()

    def last_outcome(self, intent_id: str) -> Outcome | None:
        """Recorded outcome for an id, or None if none has been recorded."""
        with self._lock:
            return self._outcomes.get(intent_id)

    def tracked_count(self) -> int:
        with self._lock:
            return len(self._outcomes)

    def fulfilment_update(self, intent_id: str, outcome: Outcome) -> None:
        with self._lock:
            self._outcomes[intent_id] = outcome
