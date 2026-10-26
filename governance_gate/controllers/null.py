"""Controller that logs and changes nothing."""
import logging

log = logging.getLogger(__name__)


class NullController:
    def apply(self, template_id: str, scope: str, idempotency_key: str) -> str:
        handle = f"null:{template_id}:{scope}:{idempotency_key}"
        log.info("NullController.apply %s (no change made)", handle)
        return handle

    def rollback(self, handle: str) -> None:
        log.info("NullController.rollback %s (no change made)", handle)
