"""Idempotent action executor. Re-evaluates the gate itself before applying."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionResult:
    status: str  # executed | denied | failed | rolled_back
    handle: str | None
    reasons: tuple[str, ...]
    record_id: str


class Executor:
    def __init__(self, gate, controller, records):
        self._gate = gate
        self._controller = controller
        self._records = records
        self._executed: dict[str, ExecutionResult] = {}  # only executed results cached

    def _record(self, status, handle, reasons, decision_id, key) -> str:
        return self._records.append(
            "action_execution",
            {"status": status, "handle": handle, "reasons": list(reasons),
             "idempotency_key": key},
            links=[decision_id] if decision_id else [],
        )

    def _safe_record(self, *args) -> str:
        try:
            return self._record(*args)
        except Exception:
            return ""

    def execute(self, proposal, idempotency_key, now) -> ExecutionResult:
        if isinstance(idempotency_key, str) and idempotency_key in self._executed:
            return self._executed[idempotency_key]

        decision = self._gate.evaluate(proposal, now)  # always fresh, never trusted from earlier
        if not isinstance(idempotency_key, str) or not idempotency_key:
            reasons = ("invalid idempotency key",)
            return ExecutionResult("denied", None, reasons,
                                   self._safe_record("denied", None, reasons, decision.record_id, None))
        if not decision.allowed:
            return ExecutionResult("denied", None, decision.reasons,
                                   self._safe_record("denied", None, decision.reasons,
                                                     decision.record_id, idempotency_key))

        try:
            handle = self._controller.apply(proposal.template_id, proposal.scope, idempotency_key)
        except Exception as exc:
            reasons = (f"controller error: {type(exc).__name__}",)
            return ExecutionResult("failed", None, reasons,
                                   self._safe_record("failed", None, reasons,
                                                     decision.record_id, idempotency_key))

        try:
            record_id = self._record("executed", handle, (), decision.record_id, idempotency_key)
        except Exception as exc:
            return self._rollback(handle, exc, decision.record_id, idempotency_key)
        result = ExecutionResult("executed", handle, (), record_id)
        self._executed[idempotency_key] = result
        return result

    def _rollback(self, handle, cause, decision_id, key) -> ExecutionResult:
        base = f"post-apply error: {type(cause).__name__}"
        try:
            self._controller.rollback(handle)
        except Exception as exc:
            reasons = (base, f"rollback failed: {type(exc).__name__}")
            return ExecutionResult("failed", handle, reasons,
                                   self._safe_record("failed", handle, reasons, decision_id, key))
        reasons = (base,)
        return ExecutionResult("rolled_back", handle, reasons,
                               self._safe_record("rolled_back", handle, reasons, decision_id, key))
