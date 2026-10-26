from outcome_assurance.evaluate import Outcome, Verdict, evaluate_intent
from data_contracts.envelope import ContextEnvelope, Quality, StateCategory


def env(value, stale=False, missing=False, eid="x"):
    return ContextEnvelope(eid, StateCategory.OBSERVED, value, "t", None,
                           Quality(stale=stale, missing=missing))


def intent(op="==", target=0.0, ifaces=(("r1", "e0"), ("r2", "e1"))):
    return {"metric": "input_drops", "operator": op, "target_value": target,
            "interfaces": [{"device": d, "interface_name": i} for d, i in ifaces],
            "state": "observed", "source": "s"}


def test_all_within_target_success():
    v = evaluate_intent(intent(), {("r1", "e0"): env(0), ("r2", "e1"): env(0)})
    assert v.outcome == Outcome.SUCCESS
    assert len(v.per_interface) == 2
    assert v.per_interface[0]["outcome"] == "success"


def test_one_over_target_failure_even_with_missing():
    v = evaluate_intent(intent(), {("r1", "e0"): env(5)})
    assert v.outcome == Outcome.FAILURE


def test_one_missing_rest_passing_inconclusive():
    v = evaluate_intent(intent(), {("r1", "e0"): env(0), ("r2", "e1"): env(None, missing=True)})
    assert v.outcome == Outcome.INCONCLUSIVE
    assert any("missing" in r for r in v.reasons)


def test_absent_interface_is_missing():
    v = evaluate_intent(intent(), {("r1", "e0"): env(0)})
    assert v.outcome == Outcome.INCONCLUSIVE
    assert any("missing" in r for r in v.reasons)


def test_stale_inconclusive():
    v = evaluate_intent(intent(), {("r1", "e0"): env(99, stale=True), ("r2", "e1"): env(0)})
    assert v.outcome == Outcome.INCONCLUSIVE
    assert any("stale" in r for r in v.reasons)


def test_unknown_operator():
    v = evaluate_intent(intent(op="~="), {("r1", "e0"): env(0), ("r2", "e1"): env(0)})
    assert v.outcome == Outcome.INCONCLUSIVE
    assert any("~=" in r for r in v.reasons)


def test_empty_observations_and_interfaces():
    assert evaluate_intent(intent(), {}).outcome == Outcome.INCONCLUSIVE
    v = evaluate_intent(intent(ifaces=()), {("r1", "e0"): env(0)})
    assert v.outcome == Outcome.INCONCLUSIVE and v.reasons


def test_operators():
    for op, val, exp in [("<", 1, Outcome.SUCCESS), ("<=", 2, Outcome.SUCCESS),
                         (">=", 1, Outcome.FAILURE), (">", 2, Outcome.FAILURE)]:
        v = evaluate_intent(intent(op=op, target=2.0, ifaces=(("r1", "e0"),)),
                            {("r1", "e0"): env(val)})
        assert v.outcome == exp, op
