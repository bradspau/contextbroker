import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from outcome_assurance.evaluate import Outcome
from outcome_assurance.tmf921_report import interpret_report

FIX = Path(__file__).parent / "fixtures" / "tmf921"
NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
M = "http://example.org/metrics#DownstreamBandwidthMetric"
FRESH = {M: NOW - timedelta(seconds=10)}


def load(name):
    return json.loads((FIX / f"report_{name}.json").read_text())


def run(report, observed_at=FRESH, **kw):
    return interpret_report(report, now=NOW, observed_at=observed_at, **kw)


def test_fulfilled_fresh_is_success():
    assert run(load("fulfilled")).outcome is Outcome.SUCCESS


def test_violation_is_failure():
    v = run(load("degraded_violation"))
    assert v.outcome is Outcome.FAILURE
    assert v.per_interface[0]["reason"] == "goal not met"


def test_no_observation_inconclusive_without_echo():
    v = run(load("degraded_no_observation"))
    assert v.outcome is Outcome.INCONCLUSIVE
    assert v.reasons == ["no observation"]
    assert "Downstream" not in json.dumps(v.reasons)


def test_unit_mismatch_inconclusive():
    v = run(load("degraded_unit_mismatch"))
    assert v.outcome is Outcome.INCONCLUSIVE and v.reasons == ["unit mismatch"]


def test_fulfilled_stale_is_inconclusive():
    old = {M: NOW - timedelta(seconds=10_000)}
    v = run(load("fulfilled"), observed_at=old)
    assert v.outcome is Outcome.INCONCLUSIVE
    assert v.reasons == ["stale observation: DownstreamBandwidthMetric"]


def test_fulfilled_none_future_naive_stale():
    for ts in (None, NOW + timedelta(seconds=60), datetime(2026, 10, 27, 11, 59, 59)):
        assert run(load("fulfilled"), observed_at={M: ts}).outcome is Outcome.INCONCLUSIVE


def test_fulfilled_observed_at_missing_fails_closed():
    assert run(load("fulfilled"), observed_at=None).outcome is Outcome.INCONCLUSIVE
    assert run(load("fulfilled"), observed_at={}).outcome is Outcome.INCONCLUSIVE


def test_metric_name_sanitised():
    v = run(load("fulfilled"), observed_at={"http://x/y#Bad<script>\n": None})
    assert v.reasons == ["stale observation: Bad_script__"]


def test_violation_not_downgraded_by_staleness():
    assert run(load("degraded_violation"), observed_at=None).outcome is Outcome.FAILURE


def test_failure_plus_missing_is_failure():
    r = load("degraded_violation")
    r["intentHandlingReason"] = ("Conditions not met: atLeast: no observation: X; "
                                 "50 Mbps >= 100 Mbps: FAIL")
    assert run(r).outcome is Outcome.FAILURE


def test_unrecognised_clause_and_unknown_state_inconclusive():
    r = load("degraded_violation")
    r["intentHandlingReason"] = "Conditions not met: weird"
    assert run(r).outcome is Outcome.INCONCLUSIVE
    r2 = load("fulfilled")
    r2["intentHandlingState"] = "Weird"
    assert run(r2).outcome is Outcome.INCONCLUSIVE


def test_garbage_types_inconclusive_no_exception():
    for bad in (None, 5, "x", []):
        assert run(bad).outcome is Outcome.INCONCLUSIVE
    for field, val in (("expression", 5), ("intentHandlingState", 3),
                       ("intentHandlingReason", 7)):
        r = load("fulfilled")
        r[field] = val
        assert run(r).outcome is Outcome.INCONCLUSIVE
    assert interpret_report(load("fulfilled"), now=datetime(2026, 1, 1),
                            observed_at=FRESH).outcome is Outcome.INCONCLUSIVE


TTL = """@prefix icm: <http://tio.models.tmforum.org/tio/v3.2.0/IntentCommonModel/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <http://example.org/> .
ex:c1 a icm:ConditionReport ; icm:result "true" .
"""


def _with_expr(name, text):
    r = load(name)
    r["expression"] = {"@type": "TurtleExpression", "expressionValue": text}
    return r


def test_turtle_agreeing_is_success():
    assert run(_with_expr("fulfilled", TTL)).outcome is Outcome.SUCCESS


def test_turtle_disagreement_inconclusive():
    ttl = TTL.replace('"true"', '"false" ; icm:reason "bound exceeded"')
    assert run(_with_expr("fulfilled", ttl)).outcome is Outcome.INCONCLUSIVE


def test_unparseable_turtle_inconclusive():
    v = run(_with_expr("fulfilled", "this is ((( not turtle"))
    assert v.outcome is Outcome.INCONCLUSIVE and v.reasons == ["unparseable report"]


def test_oversized_turtle_inconclusive():
    v = run(_with_expr("fulfilled", "#" * 70_000))
    assert v.reasons == ["unparseable report"]
