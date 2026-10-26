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
    def __init__(self, apply_exc=None, rollback_exc=None):
        self.applied, self.rolled, self.apply_exc, self.rollback_exc = [], [], apply_exc, rollback_exc

    def apply(self, template_id, scope, idempotency_key):
        if self.apply_exc:
            raise self.apply_exc
        self.applied.append((template_id, scope, idempotency_key))
        return "h-1"

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
    return Executor(gate, ctrl, rec), gate, rec


def test_executes_and_records_linked(env):
    ex, gate, rec = make(env, Spy())
    r = ex.execute(proposal(), "k1", NOW)
    assert isinstance(r, ExecutionResult) and r.status == "executed" and r.handle == "h-1"
    row = rec.get(r.record_id)
    assert row["kind"] == "action_execution" and row["payload"]["status"] == "executed"
    assert len(row["links"]) == 1 and rec.get(row["links"][0])["kind"] == "gate_decision"


def test_gate_reevaluated_at_execute(env):
    ex, gate, _ = make(env, Spy())
    gate.evaluate(proposal(), NOW)  # prior decision, must not be trusted
    before = gate.calls
    ex.execute(proposal(), "k1", NOW)
    assert gate.calls == before + 1


def test_denied_never_calls_controller(env):
    spy = Spy()
    ex, _, rec = make(env, spy)
    r = ex.execute(proposal("site/zzz"), "k1", NOW)
    assert r.status == "denied" and r.handle is None and r.reasons
    assert spy.applied == []
    assert rec.get(r.record_id)["kind"] == "action_execution"


def test_idempotent_executed_cached(env):
    spy = Spy()
    ex, gate, _ = make(env, spy)
    r1 = ex.execute(proposal(), "k1", NOW)
    n = gate.calls
    r2 = ex.execute(proposal(), "k1", NOW)
    assert r2 == r1 and len(spy.applied) == 1 and gate.calls == n


def test_denied_not_cached(env):
    spy = Spy()
    ex, _, res = make(env, spy)
    res_book = env[2]
    res_book.release("L1", "site/a")
    assert ex.execute(proposal(), "k1", NOW).status == "denied"
    res_book.reserve("L1", "site/a", 600, NOW)
    assert ex.execute(proposal(), "k1", NOW).status == "executed"
    assert len(spy.applied) == 1


def test_controller_exception_fails(env):
    spy = Spy(apply_exc=RuntimeError("boom"))
    ex, _, rec = make(env, spy)
    r = ex.execute(proposal(), "k1", NOW)
    assert r.status == "failed" and r.handle is None and any("RuntimeError" in x for x in r.reasons)
    assert spy.rolled == []
    assert rec.get(r.record_id)["payload"]["status"] == "failed"
    assert ex.execute(proposal(), "k1", NOW).status == "failed"  # failures not cached


def test_post_apply_failure_rolls_back(env):
    spy = Spy()
    gate, rec, _ = env

    class FlakyRecords:
        def __init__(self):
            self.n = 0

        def append(self, kind, payload, links=[]):
            if kind == "action_execution" and payload["status"] == "executed":
                raise OSError("disk")
            return rec.append(kind, payload, links)

    gate2 = Gate(gate._catalogue, gate._reservations, rec)
    r = Executor(gate2, spy, FlakyRecords()).execute(proposal(), "k1", NOW)
    assert spy.rolled == ["h-1"] and r.status == "rolled_back"


def test_rollback_failure_reported(env):
    spy = Spy(rollback_exc=RuntimeError("nope"))
    gate, rec, _ = env

    class FlakyRecords:
        def append(self, kind, payload, links=[]):
            if payload.get("status") == "executed":
                raise OSError("disk")
            return rec.append(kind, payload, links)

    r = Executor(Gate(gate._catalogue, gate._reservations, rec), spy, FlakyRecords()).execute(
        proposal(), "k1", NOW)
    assert r.status == "failed" and any("rollback failed" in x for x in r.reasons)


def test_bad_idempotency_key_denied(env):
    spy = Spy()
    ex, _, _ = make(env, spy)
    assert ex.execute(proposal(), "", NOW).status == "denied"
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
