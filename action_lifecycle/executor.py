"""Idempotent action executor. Snapshots the proposal once, re-evaluates the gate
itself immediately before applying, and never trusts a prior decision."""
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from governance_gate.policies.action_readiness import ActionProposal

REUSED = "idempotency key reused with a different proposal"


@dataclass(frozen=True)
class ExecutionResult:
    status: str  # executed | denied | failed | rolled_back
    handle: str | None
    reasons: tuple[str, ...]
    record_id: str


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _snapshot(proposal) -> ActionProposal | None:
    """Read each field exactly once from an exact ActionProposal; None if malformed."""
    if type(proposal) is not ActionProposal:
        return None
    template_id, scope = proposal.template_id, proposal.scope
    loop_id, outcome, ctx = proposal.loop_id, proposal.expected_outcome, proposal.context
    if any(type(v) is not str or v == "" for v in (template_id, scope, loop_id, outcome)):
        return None
    if type(ctx) is not tuple:
        return None
    return ActionProposal(template_id, scope, loop_id, tuple(ctx), outcome)


def _fingerprint(snap: ActionProposal) -> tuple[str, str, str, str]:
    return (snap.template_id, snap.scope, snap.loop_id, snap.expected_outcome)


class Executor:
    def __init__(self, gate, controller, records,
                 clock: Callable[[], datetime] = _default_clock):
        self._gate = gate
        self._controller = controller
        self._records = records
        self._clock = clock
        self._cond = threading.Condition()
        self._inflight: set[str] = set()
        self._executed: dict[str, tuple[tuple, ExecutionResult]] = {}

    # -- recording -------------------------------------------------------
    def _record(self, status, handle, reasons, decision_id, key, fp) -> str:
        return self._records.append(
            "action_execution",
            {"status": status, "handle": handle, "reasons": list(reasons),
             "idempotency_key": key, "fingerprint": list(fp) if fp else None},
            links=[decision_id] if decision_id else [],
        )

    def _safe_record(self, *args) -> str:
        try:
            return self._record(*args)
        except Exception:
            return ""

    def _denied(self, reasons, decision_id, key, fp) -> ExecutionResult:
        reasons = tuple(reasons)
        return ExecutionResult("denied", None, reasons,
                               self._safe_record("denied", None, reasons, decision_id, key, fp))

    # -- idempotency -----------------------------------------------------
    def _prior(self, key, fp) -> ExecutionResult | None:
        """Prior executed result for key (memory, then durable). A different
        fingerprint yields a denial. Call with the condition held."""
        hit = self._executed.get(key)
        if hit is None:
            for rec in reversed(self._records.find("action_execution", "idempotency_key", key)):
                p = rec["payload"]
                if p.get("status") == "executed" and type(p.get("handle")) is str:
                    fpr = tuple(p["fingerprint"]) if p.get("fingerprint") else None
                    hit = (fpr, ExecutionResult("executed", p["handle"], (), rec["id"]))
                    self._executed[key] = hit
                    break
        if hit is None:
            return None
        if fp is None or hit[0] != fp:
            return self._denied([REUSED], None, key, fp)
        return hit[1]

    # -- main ------------------------------------------------------------
    def execute(self, proposal, idempotency_key) -> ExecutionResult:
        if type(idempotency_key) is not str or idempotency_key == "":
            return self._denied(["invalid idempotency key"], None, None, None)
        key = idempotency_key
        snap = _snapshot(proposal)
        fp = _fingerprint(snap) if snap is not None else None

        with self._cond:
            while key in self._inflight:
                self._cond.wait()
            try:
                prior = self._prior(key, fp)
            except Exception as exc:
                return self._denied([f"idempotency lookup failed: {type(exc).__name__}"],
                                    None, key, fp)
            if prior is not None:
                return prior
            self._inflight.add(key)
        try:
            return self._run(proposal, snap, fp, key)
        finally:
            with self._cond:
                self._inflight.discard(key)
                self._cond.notify_all()

    def _run(self, original, snap, fp, key) -> ExecutionResult:
        now = self._clock()
        # Malformed input: let the gate deny and record it; never apply.
        decision = self._gate.evaluate(snap if snap is not None else original, now)
        if snap is None or not decision.allowed:
            reasons = decision.reasons or ("malformed proposal",)
            return self._denied(reasons, decision.record_id, key, fp)

        try:
            handle = self._controller.apply(snap.template_id, snap.scope, key)
        except Exception as exc:
            return self._compensate(f"controller error: {type(exc).__name__}",
                                    snap, key, fp, decision.record_id)
        if type(handle) is not str or handle == "":
            return self._compensate("controller returned invalid handle",
                                    snap, key, fp, decision.record_id)

        try:
            record_id = self._record("executed", handle, (), decision.record_id, key, fp)
        except Exception as exc:
            return self._rollback(handle, exc, decision.record_id, key, fp)
        result = ExecutionResult("executed", handle, (), record_id)
        with self._cond:
            self._executed[key] = (fp, result)
        return result

    def _compensate(self, why, snap, key, fp, decision_id) -> ExecutionResult:
        reasons = [why]
        try:
            self._controller.compensate(snap.template_id, snap.scope, key)
        except Exception as exc:
            reasons.append(f"compensate failed: {type(exc).__name__}")
        reasons = tuple(reasons)
        return ExecutionResult("failed", None, reasons,
                               self._safe_record("failed", None, reasons, decision_id, key, fp))

    def _rollback(self, handle, cause, decision_id, key, fp) -> ExecutionResult:
        base = f"post-apply error: {type(cause).__name__}"
        try:
            self._controller.rollback(handle)
        except Exception as exc:
            reasons = (base, f"rollback failed: {type(exc).__name__}")
            return ExecutionResult("failed", handle, reasons,
                                   self._safe_record("failed", handle, reasons, decision_id, key, fp))
        reasons = (base,)
        return ExecutionResult("rolled_back", handle, reasons,
                               self._safe_record("rolled_back", handle, reasons, decision_id, key, fp))
