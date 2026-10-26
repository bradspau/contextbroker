from datetime import datetime, timedelta, timezone
import pytest
from artefact_governance.action_catalogue import ActionCatalogue, ActionTemplate
from data_contracts.envelope import ContextEnvelope, Quality, StateCategory
from decision_records.store import RecordStore
from governance_gate.gate_proxy import ActionProposal, Decision, Gate
from loop_coordination.reservations import ReservationBook

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TEMPLATE = ActionTemplate("restart", "1.0", "site/*", ("link_down",), "link_up", "revert", True)


def env(state=StateCategory.OBSERVED, **q):
    return ContextEnvelope("e", state, 1.0, "src", NOW, Quality(**q))


def proposal(**over):
    base = dict(template_id="restart", scope="site/a", loop_id="L1",
                context=(env(),), expected_outcome="link_up")
    base.update(over)
    return ActionProposal(**base)


@pytest.fixture
def parts():
    cat = ActionCatalogue([TEMPLATE])
    res = ReservationBook()
    res.reserve("L1", "site/a", 60, NOW)
    rec = RecordStore()
    return cat, res, rec, Gate(cat, res, rec)


def test_allow(parts):
    _, _, rec, gate = parts
    d = gate.evaluate(proposal(), NOW)
    assert isinstance(d, Decision) and d.allowed and d.reasons == ()
    r = rec.get(d.record_id)
    assert r["kind"] == "gate_decision"
    assert r["payload"] == {"template_id": "restart", "scope": "site/a", "loop_id": "L1",
                            "allowed": True, "reasons": []}


def test_deny_unknown_template(parts):
    d = parts[3].evaluate(proposal(template_id="nope"), NOW)
    assert not d.allowed and any("template" in r for r in d.reasons)


def test_deny_unapproved_template(parts):
    cat = ActionCatalogue([ActionTemplate("x", "1", "*", (), "o", "r", False)])
    gate = Gate(cat, parts[1], parts[2])
    assert not gate.evaluate(proposal(template_id="x"), NOW).allowed


def test_deny_scope_mismatch(parts):
    cat, res, rec, gate = parts
    res.reserve("L1", "other/b", 60, NOW)
    d = gate.evaluate(proposal(scope="other/b"), NOW)
    assert not d.allowed and any("scope" in r for r in d.reasons)


def test_deny_empty_context(parts):
    d = parts[3].evaluate(proposal(context=()), NOW)
    assert not d.allowed and any("context" in r for r in d.reasons)


@pytest.mark.parametrize("bad", [dict(stale=True), dict(missing=True), dict(conflicts=["x"])])
def test_deny_bad_envelope(parts, bad):
    d = parts[3].evaluate(proposal(context=(env(), env(**bad))), NOW)
    assert not d.allowed


def test_deny_inferred_only(parts):
    d = parts[3].evaluate(proposal(context=(env(StateCategory.INFERRED),)), NOW)
    assert not d.allowed and any("inferred" in r for r in d.reasons)


def test_inferred_plus_observed_allowed(parts):
    d = parts[3].evaluate(proposal(context=(env(StateCategory.INFERRED), env())), NOW)
    assert d.allowed


def test_deny_intended_only(parts):
    assert not parts[3].evaluate(proposal(context=(env(StateCategory.INTENDED),)), NOW).allowed


def test_deny_no_reservation(parts):
    _, res, _, gate = parts
    assert not gate.evaluate(proposal(loop_id="L2"), NOW).allowed
    assert not gate.evaluate(proposal(), NOW + timedelta(seconds=120)).allowed  # expired


def test_deny_outcome_mismatch(parts):
    d = parts[3].evaluate(proposal(expected_outcome="else"), NOW)
    assert not d.allowed and any("outcome" in r for r in d.reasons)


@pytest.mark.parametrize("bad", ["a string", None, {"template_id": "restart"}, 42])
def test_wrong_typed_proposal(parts, bad):
    d = parts[3].evaluate(bad, NOW)
    assert not d.allowed and d.reasons


@pytest.mark.parametrize("field,value", [
    ("template_id", 5), ("scope", ""), ("loop_id", None), ("expected_outcome", 1),
    ("context", [env()]), ("context", (object(),)),
])
def test_wrong_typed_fields(parts, field, value):
    assert not parts[3].evaluate(proposal(**{field: value}), NOW).allowed


def test_catalogue_exception_denies(parts):
    class Boom:
        def get(self, _):
            raise RuntimeError("x")
    gate = Gate(Boom(), parts[1], parts[2])
    d = gate.evaluate(proposal(), NOW)
    assert not d.allowed and d.reasons == ("gate error: RuntimeError",)


def test_every_decision_recorded(parts):
    _, _, rec, gate = parts
    d1 = gate.evaluate(proposal(), NOW)
    d2 = gate.evaluate(proposal(scope="zzz"), NOW)
    d3 = gate.evaluate("junk", NOW)
    assert len({d1.record_id, d2.record_id, d3.record_id}) == 3
    for d in (d1, d2, d3):
        p = rec.get(d.record_id)["payload"]
        assert p["allowed"] is d.allowed and p["reasons"] == list(d.reasons)


def test_no_model_score_input():
    import inspect
    names = set(inspect.signature(ActionProposal).parameters)
    assert not names & {"score", "confidence", "model_score"}
