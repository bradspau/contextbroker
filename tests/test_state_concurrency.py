"""Many-thread tests for the reservation book and the action-readiness check.

Each race window is widened on purpose, so code without the fix fails here every time."""
import threading
import time
from datetime import datetime, timezone

import governance_gate.gate_proxy as gate_proxy
from artefact_governance.action_catalogue import ActionCatalogue, ActionTemplate
from data_contracts.envelope import ContextEnvelope, Quality, StateCategory
from decision_records.store import RecordStore
from governance_gate.gate_proxy import ActionProposal, Gate
from loop_coordination.reservations import ReservationBook

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _run_threads(n, target):
    barrier = threading.Barrier(n)
    results = [None] * n
    errors = []

    def runner(i):
        try:
            barrier.wait()
            results[i] = target(i)
        except BaseException as exc:  # collected so the test can assert on it
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


class _SlowHolder(ReservationBook):
    def holder(self, scope, now):
        h = super().holder(scope, now)
        time.sleep(0.01)
        return h


def test_sixteen_loops_one_scope_exactly_one_holder():
    book = _SlowHolder()
    results, errors = _run_threads(16, lambda i: book.reserve(f"L{i}", "s", 60, NOW))
    assert errors == []
    assert sum(1 for r in results if r is True) == 1
    winner = results.index(True)
    assert book.holder("s", NOW) == f"L{winner}"


def test_reserve_and_release_churn_ends_empty():
    book = ReservationBook()

    def churn(i):
        for _ in range(200):
            assert book.reserve(f"L{i}", f"scope-{i}", 60, NOW) is True
            assert book.holder(f"scope-{i}", NOW) == f"L{i}"
            book.release(f"L{i}", f"scope-{i}")

    _, errors = _run_threads(16, churn)
    assert errors == []
    assert all(book.holder(f"scope-{i}", NOW) is None for i in range(16))
    assert book._held == {}


def test_each_decision_record_carries_its_own_template_version(monkeypatch):
    real = gate_proxy.check

    def slow_check(*a, **k):
        result = real(*a, **k)
        time.sleep(0.01)
        return result

    monkeypatch.setattr(gate_proxy, "check", slow_check)
    n = 8
    templates = [ActionTemplate(f"t{i}", f"{i}.0", "site/*", ("link_down",), "link_up", "revert", True)
                 for i in range(n)]
    book = ReservationBook()
    for i in range(n):
        assert book.reserve(f"L{i}", f"site/{i}", 60, NOW)
    records = RecordStore(":memory:")
    gate = Gate(ActionCatalogue(templates), book, records)

    def decide(i):
        proposal = ActionProposal(
            template_id=f"t{i}", scope=f"site/{i}", loop_id=f"L{i}",
            context=(ContextEnvelope("e", StateCategory.OBSERVED, 1.0, "src", NOW, Quality()),),
            expected_outcome="link_up")
        return gate.evaluate(proposal, NOW)

    decisions, errors = _run_threads(n, decide)
    assert errors == []
    for i, decision in enumerate(decisions):
        payload = records.get(decision.record_id)["payload"]
        assert payload["template_id"] == f"t{i}"
        assert payload["template_version"] == f"{i}.0"
        assert payload["reservation_holder"] == f"L{i}"


def test_exception_inside_the_decision_records_no_template_version():
    class Broken:
        def get(self, template_id):
            raise KeyError("boom")

    records = RecordStore(":memory:")
    book = ReservationBook()
    book.reserve("L1", "site/a", 60, NOW)
    gate = Gate(Broken(), book, records)
    proposal = ActionProposal(
        template_id="restart", scope="site/a", loop_id="L1",
        context=(ContextEnvelope("e", StateCategory.OBSERVED, 1.0, "src", NOW, Quality()),),
        expected_outcome="link_up")
    decision = gate.evaluate(proposal, NOW)
    assert decision.allowed is False
    assert decision.reasons == ("gate error: KeyError",)
    payload = records.get(decision.record_id)["payload"]
    assert payload["template_version"] is None
    assert payload["reservation_holder"] is None
