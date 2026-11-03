"""Server-side record of a fail-closed exit: the exception class and a fixed reason."""
import logging


def log_fault(logger: logging.Logger, exc: BaseException, reason: str) -> None:
    """Log one WARNING line "<reason>: <ExceptionClass>" for a fail-closed handler.

    `reason` must be a fixed string built only from pinned metric names and
    numbers. The exception class lets an operator tell a network fault from a
    parser fault (D-17). The exception text, URL and traceback are never
    logged: they can carry server text, so this never passes exc_info, never
    formats str(exc) and is not used with logger.exception.
    """
    logger.warning("%s: %s", reason, type(exc).__name__)
