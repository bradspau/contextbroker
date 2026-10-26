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
    p = r["payload"]
    assert p["template_id"] == "restart" and p["scope"] == "site/a" and p["loop_id"] == "L1"
    assert p["allowed"] is True and p["reasons"] == []
    assert p["template_version"] == "1.0"
    assert p["evidence"] == [{"entity_id": "e", "state": "observed", "observed_at": NOW.isoformat()}]
    assert p["reservation_holder"] == "L1"
    assert p["evaluated_at"] == NOW.isoformat()
    assert p["max_age_s"] == 300
    import json
    json.dumps(p)


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


# ---- hardening regressions ----
class Yes(str):
    __ne__ = lambda s, o: o == ""  # noqa: E731  reviewer probe: != always False-ish
    __eq__ = lambda s, o: True  # noqa: E731
    __hash__ = str.__hash__


def test_str_subclass_outcome_denied(parts):
    d = parts[3].evaluate(proposal(expected_outcome=Yes("something else")), NOW)
    assert not d.allowed


@pytest.mark.parametrize("field", ["template_id", "scope", "loop_id"])
def test_str_subclass_fields_denied(parts, field):
    class S(str):
        pass
    val = S({"template_id": "restart", "scope": "site/a", "loop_id": "L1"}[field])
    assert not parts[3].evaluate(proposal(**{field: val}), NOW).allowed


def test_proposal_subclass_denied(parts):
    class P(ActionProposal):
        pass
    p = P("restart", "site/a", "L1", (env(),), "link_up")
    assert not parts[3].evaluate(p, NOW).allowed


def test_envelope_subclass_and_state_exactness(parts):
    class E(ContextEnvelope):
        pass
    e = E("e", StateCategory.OBSERVED, 1.0, "s", NOW, Quality())
    assert not parts[3].evaluate(proposal(context=(e,)), NOW).allowed
    bad = ContextEnvelope("e", "observed", 1.0, "s", NOW, Quality())
    assert not parts[3].evaluate(proposal(context=(bad,)), NOW).allowed
    q = ContextEnvelope("e", StateCategory.OBSERVED, 1.0, "s", NOW, Quality(stale=0))
    assert not parts[3].evaluate(proposal(context=(q,)), NOW).allowed


def aged(delta, state=StateCategory.OBSERVED, at="auto"):
    return ContextEnvelope("e", state, 1.0, "s", NOW - delta if at == "auto" else at, Quality())


def test_old_observation_denied_even_if_quality_clean(parts):
    d = parts[3].evaluate(proposal(context=(aged(timedelta(days=1)),)), NOW)
    assert not d.allowed and any("fresh" in r for r in d.reasons)


def test_freshness_boundaries(parts):
    g = parts[3]
    assert g.evaluate(proposal(context=(aged(timedelta(seconds=300)),)), NOW).allowed
    assert not g.evaluate(proposal(context=(aged(timedelta(seconds=301)),)), NOW).allowed
    assert g.evaluate(proposal(context=(aged(timedelta(seconds=-5)),)), NOW).allowed
    assert not g.evaluate(proposal(context=(aged(timedelta(seconds=-6)),)), NOW).allowed


def test_missing_or_naive_observed_at_denied(parts):
    g = parts[3]
    assert not g.evaluate(proposal(context=(aged(None, at=None),)), NOW).allowed
    assert not g.evaluate(proposal(context=(aged(None, at=datetime(2026, 1, 1)),)), NOW).allowed


def test_configured_old_denied_and_max_age_configurable(parts):
    cat, res, rec, _ = parts
    old = aged(timedelta(seconds=600), StateCategory.CONFIGURED)
    assert not Gate(cat, res, rec).evaluate(proposal(context=(old,)), NOW).allowed
    assert Gate(cat, res, rec, max_age_s=900).evaluate(proposal(context=(old,)), NOW).allowed


def test_naive_now_denied(parts):
    d = parts[3].evaluate(proposal(), datetime(2026, 1, 1))
    assert not d.allowed and d.record_id


def test_deny_record_has_reconstruction_fields(parts):
    _, _, rec, gate = parts
    d = gate.evaluate(proposal(template_id="nope"), NOW)
    p = rec.get(d.record_id)["payload"]
    assert p["allowed"] is False and p["template_version"] is None
    assert p["reservation_holder"] == "L1"
    assert p["evidence"][0]["entity_id"] == "e"
    assert p["evaluated_at"] == NOW.isoformat()


@pytest.mark.parametrize("scope", ["site/*", "site/a?", "site/[ab]", "site/../x", ".."])
def test_deny_wildcard_or_traversal_scope(parts, scope):
    cat, res, rec, gate = parts
    res.reserve("L1", scope, 60, NOW)
    d = gate.evaluate(proposal(scope=scope), NOW)
    assert not d.allowed and any("wildcard" in r for r in d.reasons)
