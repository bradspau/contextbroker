import re
from datetime import datetime, timezone
from pathlib import Path
import pytest
from artefact_governance.action_catalogue import ActionCatalogue, ActionTemplate
from action_lifecycle.executor import ExecutionResult, Executor
from data_contracts.envelope import ContextEnvelope, Quality, StateCategory
from decision_records.store import RecordStore
from governance_gate.controllers.base import Controller
from governance_gate.controllers.null import NullController
from governance_gate.gate_proxy import ActionProposal, Gate
from loop_coordination.reservations import ReservationBook

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TEMPLATE = ActionTemplate("restart", "1.0", "site/*", (), "link_up", "revert", True)
ROOT = Path(__file__).resolve().parent.parent


def proposal(scope="site/a"):
    e = ContextEnvelope("e", StateCategory.OBSERVED, 1.0, "s", NOW, Quality())
    return ActionProposal("restart", scope, "L1", (e,), "link_up")


class CountingGate(Gate):
    calls = 0

    def evaluate(self, proposal, now):
        self.calls += 1
        return super().evaluate(proposal, now)


class Spy:
    def __init__(self, apply_exc=None, rollback_exc=None, compensate_exc=None, handle="h-1"):
        self.applied, self.rolled, self.compensated = [], [], []
        self.apply_exc, self.rollback_exc, self.compensate_exc = apply_exc, rollback_exc, compensate_exc
        self.handle = handle

    def compensate(self, template_id, scope, idempotency_key):
        self.compensated.append((template_id, scope, idempotency_key))
        if self.compensate_exc:
            raise self.compensate_exc

    def apply(self, template_id, scope, idempotency_key):
        if self.apply_exc:
            raise self.apply_exc
        self.applied.append((template_id, scope, idempotency_key))
        return self.handle

    def rollback(self, handle):
        self.rolled.append(handle)
        if self.rollback_exc:
            raise self.rollback_exc


@pytest.fixture
def env():
    res = ReservationBook()
    res.reserve("L1", "site/a", 600, NOW)
    rec = RecordStore()
    gate = CountingGate(ActionCatalogue([TEMPLATE]), res, rec)
    return gate, rec, res


def make(env, ctrl):
    gate, rec, _ = env
    return Executor(gate, ctrl, rec, clock=lambda: NOW), gate, rec


def test_executes_and_records_linked(env):
    ex, gate, rec = make(env, Spy())
    r = ex.execute(proposal(), "k1")
    assert isinstance(r, ExecutionResult) and r.status == "executed" and r.handle == "h-1"
    row = rec.get(r.record_id)
    assert row["kind"] == "action_execution" and row["payload"]["status"] == "executed"
    assert len(row["links"]) == 1 and rec.get(row["links"][0])["kind"] == "gate_decision"


def test_gate_reevaluated_at_execute(env):
    ex, gate, _ = make(env, Spy())
    gate.evaluate(proposal(), NOW)  # prior decision, must not be trusted
    before = gate.calls
    ex.execute(proposal(), "k1")
    assert gate.calls == before + 1


def test_denied_never_calls_controller(env):
    spy = Spy()
    ex, _, rec = make(env, spy)
    r = ex.execute(proposal("site/zzz"), "k1")
    assert r.status == "denied" and r.handle is None and r.reasons
    assert spy.applied == []
    assert rec.get(r.record_id)["kind"] == "action_execution"


def test_idempotent_executed_cached(env):
    spy = Spy()
    ex, gate, _ = make(env, spy)
    r1 = ex.execute(proposal(), "k1")
    n = gate.calls
    r2 = ex.execute(proposal(), "k1")
    assert r2 == r1 and len(spy.applied) == 1 and gate.calls == n


def test_denied_not_cached(env):
    spy = Spy()
    ex, _, res = make(env, spy)
    res_book = env[2]
    res_book.release("L1", "site/a")
    assert ex.execute(proposal(), "k1").status == "denied"
    res_book.reserve("L1", "site/a", 600, NOW)
    assert ex.execute(proposal(), "k1").status == "executed"
    assert len(spy.applied) == 1


def test_controller_exception_fails(env):
    spy = Spy(apply_exc=RuntimeError("boom"))
    ex, _, rec = make(env, spy)
    r = ex.execute(proposal(), "k1")
    assert r.status == "failed" and r.handle is None and any("RuntimeError" in x for x in r.reasons)
    assert spy.rolled == [] and spy.compensated == [("restart", "site/a", "k1")]
    assert rec.get(r.record_id)["payload"]["status"] == "failed"
    assert ex.execute(proposal(), "k1").status == "failed"  # failures not cached


def test_post_apply_failure_rolls_back(env):
    spy = Spy()
    gate, rec, _ = env

    class FlakyRecords:
        def find(self, *a):
            return rec.find(*a)

        def append(self, kind, payload, links=[]):
            if kind == "action_execution" and payload["status"] == "executed":
                raise OSError("disk")
            return rec.append(kind, payload, links)

    gate2 = Gate(gate._catalogue, gate._reservations, rec)
    r = Executor(gate2, spy, FlakyRecords(), clock=lambda: NOW).execute(proposal(), "k1")
    assert spy.rolled == ["h-1"] and r.status == "rolled_back"


def test_rollback_failure_reported(env):
    spy = Spy(rollback_exc=RuntimeError("nope"))
    gate, rec, _ = env

    class FlakyRecords:
        def find(self, *a):
            return rec.find(*a)

        def append(self, kind, payload, links=[]):
            if payload.get("status") == "executed":
                raise OSError("disk")
            return rec.append(kind, payload, links)

    r = Executor(Gate(gate._catalogue, gate._reservations, rec), spy, FlakyRecords(),
                 clock=lambda: NOW).execute(
        proposal(), "k1")
    assert r.status == "failed" and any("rollback failed" in x for x in r.reasons)


def test_bad_idempotency_key_denied(env):
    spy = Spy()
    ex, _, _ = make(env, spy)
    assert ex.execute(proposal(), "").status == "denied"
    assert spy.applied == []


def test_null_controller(caplog):
    c = NullController()
    assert isinstance(c, Controller)
    with caplog.at_level("INFO"):
        h = c.apply("restart", "site/a", "k")
    assert h == c.apply("restart", "site/a", "k") and h
    assert caplog.records
    c.rollback(h)


def test_only_action_lifecycle_imports_controllers():
    pat = re.compile(r"^\s*(from|import)\s+(governance_gate\.controllers|action_lifecycle)", re.M)
    offenders = []
    for f in ROOT.rglob("*.py"):
        rel = f.relative_to(ROOT).parts
        if rel[0] in ("venv", ".git") or rel[0] == "tests" or rel[0] in ("action_lifecycle", "governance_gate"):
            continue
        if pat.search(f.read_text()):
            offenders.append(str(f))
    assert offenders == []
    for f in (ROOT / "agent_harness").glob("*.py"):
        assert not pat.search(f.read_text())


def test_compensate_failure_recorded(env):
    spy = Spy(apply_exc=RuntimeError("boom"), compensate_exc=OSError("x"))
    ex, _, _ = make(env, spy)
    r = ex.execute(proposal(), "k1")
    assert r.status == "failed" and any("compensate failed: OSError" in x for x in r.reasons)


@pytest.mark.parametrize("bad", ["", None, 5])
def test_invalid_handle_fails_and_compensates(env, bad):
    spy = Spy(handle=bad)
    ex, _, _ = make(env, spy)
    r = ex.execute(proposal(), "k1")
    assert r.status == "failed" and spy.compensated and spy.rolled == []


def test_null_controller_compensate_noop():
    NullController().compensate("t", "s", "k")


def test_toctou_snapshot_single_read(env):
    spy = Spy()
    ex, gate, rec = make(env, spy)
    gate_calls = []
    orig = gate.evaluate
    gate.evaluate = lambda p, n: (gate_calls.append(p), orig(p, n))[1]

    class Shifty(ActionProposal):
        reads = 0

        def __getattribute__(self, name):
            v = object.__getattribute__(self, name)
            if name == "scope":
                cls = type(self)
                cls.reads += 1
                if cls.reads > 1:
                    return "site/evil"
            return v

    p = Shifty("restart", "site/a", "L1", proposal().context, "link_up")
    r = ex.execute(p, "k1")
    for _, scope, _ in spy.applied:
        assert scope == "site/a"
    assert r.status in ("denied", "executed")


def test_same_key_different_proposal_denied(env):
    spy = Spy()
    ex, gate, rec = make(env, spy)
    env[2].reserve("L1", "site/b", 600, NOW)
    r1 = ex.execute(proposal("site/a"), "k1")
    r2 = ex.execute(proposal("site/b"), "k1")
    assert r1.status == "executed" and r2.status == "denied"
    assert "idempotency key reused with a different proposal" in r2.reasons
    assert len(spy.applied) == 1
    assert rec.get(r2.record_id)["kind"] == "action_execution"
    assert ex.execute(proposal("site/a"), "k1") == r1  # original still replays


def test_durable_idempotency_across_restart(env):
    spy1, spy2 = Spy(), Spy()
    ex1, gate, rec = make(env, spy1)
    r1 = ex1.execute(proposal(), "k1")
    ex2 = Executor(gate, spy2, rec, clock=lambda: NOW)
    r2 = ex2.execute(proposal(), "k1")
    assert r2.status == "executed" and r2.handle == r1.handle and spy2.applied == []
    assert ex2.execute(proposal("site/b"), "k1").status == "denied"


def test_concurrent_same_key_applies_once(env):
    import threading
    import time

    class Slow(Spy):
        def apply(self, *a):
            time.sleep(0.2)
            return super().apply(*a)
    spy = Slow()
    ex, _, _ = make(env, spy)
    out = []
    ts = [threading.Thread(target=lambda: out.append(ex.execute(proposal(), "k1"))) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(spy.applied) == 1
    assert [r.status for r in out] == ["executed", "executed"] and out[0] == out[1]


def test_clock_injected_and_no_now_param(env):
    import inspect
    assert "now" not in inspect.signature(Executor.execute).parameters
    gate, rec, res = env
    late = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)  # reservation (600s) expired by then
    spy = Spy()
    r = Executor(gate, spy, rec, clock=lambda: late).execute(proposal(), "k9")
    assert r.status == "denied" and spy.applied == []


def test_key_validated_before_gate(env):
    ex, gate, _ = make(env, Spy())
    for bad in ("", None, 5):
        assert ex.execute(proposal(), bad).status == "denied"
    assert gate.calls == 0


def test_idempotency_lookup_failure_fails_closed(env):
    gate, rec, _ = env
    spy = Spy()

    class Broken:
        def find(self, *a):
            raise OSError("db")

        def append(self, *a, **k):
            return rec.append(*a, **k)
    r = Executor(gate, spy, Broken(), clock=lambda: NOW).execute(proposal(), "k1")
    assert r.status == "denied" and spy.applied == []
