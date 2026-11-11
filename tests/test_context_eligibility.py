"""Eligibility of the requested context classes: pure functions, fixed reasons, apart from the verdict.

The goals tests read fluents written by the real builders through the real fetch_fluents, with
only requests.post replaced by an rdflib-backed stand-in (tests.fluent_helpers.GraphFuseki).
"""
import ast
import math
import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from broker_engine import context_eligibility as ce
from broker_engine.binding_resolver import fetch_fluents, resolve_binding, select_binding
from data_contracts.context_contract import ClassEligibility
from data_contracts.metric_map import MetricMapping
from tests.fluent_helpers import (IF0, IF1, IFACES_TTL, JIT, LAT, QUERY_URL, T0, T1, T2,
                                  GraphFuseki, bind, correct_placement, replace_goal)

POST = "broker_engine.binding_resolver.requests.post"
MAPPING = MetricMapping("latency", LAT, "ms", "<")
ALLOW = lambda device: True  # noqa: E731


def fuseki() -> GraphFuseki:
    fx = GraphFuseki()
    fx.graph.parse(data=IFACES_TTL, format="turtle")
    return fx


def read(fx, intent_id="i1"):
    with patch(POST, side_effect=fx.post):
        return fetch_fluents(intent_id, QUERY_URL)


def goals(fx, *, valid_at, known_at=None, limit_s=300, allowed=ALLOW, metric_map=None):
    fluents, interfaces = read(fx)
    return ce.goals_eligibility(
        fluents, interfaces, {LAT: MAPPING} if metric_map is None else metric_map,
        valid_at=valid_at, known_at=known_at, limit_s=limit_s, allowed=allowed)


# ---- the tracer: a goal replaced inside the evidence window ------------------------------


def replaced_goal_scenario():
    fx = fuseki()
    binding = bind(fx, "i1", LAT, 100.0, IF0, T0)
    replace_goal(fx, binding, 150.0, T1, T0)
    return fx, binding


def test_a_goal_replaced_inside_the_window_is_ineligible_with_a_fixed_reason():
    fx, _ = replaced_goal_scenario()
    got = goals(fx, valid_at=T1 + timedelta(seconds=100))
    assert got == ClassEligibility(result="ineligible",
                                   reasons=["goal changed within window: LatencyMetric"])


def test_the_same_goal_an_hour_after_the_change_is_eligible():
    fx, _ = replaced_goal_scenario()
    got = goals(fx, valid_at=T1 + timedelta(hours=1))
    assert got == ClassEligibility(result="eligible", reasons=[])


def test_a_tighter_limit_leaves_the_change_outside_the_window():
    fx, _ = replaced_goal_scenario()
    got = goals(fx, valid_at=T1 + timedelta(seconds=100), limit_s=60)
    assert got.result == "eligible"


def test_a_correction_is_not_a_change():
    fx = fuseki()
    binding = bind(fx, "i1", LAT, 100.0, IF0, T0)
    correct_placement(fx, binding, IF1, T2, T0, T0)
    got = goals(fx, valid_at=T2 + timedelta(seconds=100))
    assert got == ClassEligibility(result="eligible", reasons=[])


def test_before_the_new_goal_was_recorded_the_goal_is_a_gap():
    fx, _ = replaced_goal_scenario()
    got = goals(fx, valid_at=T1 + timedelta(seconds=100), known_at=T1 - timedelta(hours=1))
    # The closed goal was already recorded then but no longer covers V: the broker knew no goal.
    assert got == ClassEligibility(result="gap", reasons=["no goal in force: LatencyMetric"])


def test_no_fluents_at_all_is_a_gap_for_the_intent():
    got = ce.goals_eligibility({}, {}, {LAT: MAPPING}, valid_at=T1, known_at=None,
                               limit_s=300, allowed=ALLOW)
    assert got == ClassEligibility(result="gap", reasons=[ce.NO_BINDING])


# ---- overlapping ---------------------------------------------------------------------------


def test_overlapping_keeps_known_fluents_that_meet_the_window():
    fx, _ = replaced_goal_scenario()
    fluents, _ = read(fx)
    goal = fluents[(LAT, "goal")]
    assert len(goal) == 2
    hi = T1 + timedelta(seconds=100)
    assert len(ce.overlapping(goal, hi - timedelta(seconds=300), hi, None)) == 2
    assert len(ce.overlapping(goal, hi - timedelta(seconds=60), hi, None)) == 1  # T1 is outside
    # Window entirely after the change: only the open goal remains.
    later = T1 + timedelta(hours=2)
    assert len(ce.overlapping(goal, later - timedelta(seconds=300), later, None)) == 1
    # Not known yet at K: nothing.
    assert ce.overlapping(goal, T0, hi, T0 - timedelta(hours=1)) == []


def test_overlapping_end_is_exclusive_and_start_inclusive():
    fx, _ = replaced_goal_scenario()
    fluents, _ = read(fx)
    goal = fluents[(LAT, "goal")]
    # The old goal ends exactly at T1: a window starting at T1 does not meet it.
    assert [g.value for g in ce.overlapping(goal, T1, T1 + timedelta(seconds=10), None)] == [150.0]
    # A window ending exactly at T1 meets the new goal (start <= hi).
    assert {g.value for g in ce.overlapping(goal, T1 - timedelta(seconds=10), T1, None)} == \
        {100.0, 150.0}


# ---- select_binding is the selection half of resolve_binding --------------------------------


def test_select_binding_equals_resolve_binding_on_the_same_graph():
    fx, _ = replaced_goal_scenario()
    valid_at = T1 + timedelta(hours=1)
    fluents, interfaces = read(fx)
    with patch(POST, side_effect=fx.post):
        resolved = resolve_binding("i1", QUERY_URL, valid_at=valid_at)
    assert select_binding("i1", fluents, interfaces, valid_at=valid_at, known_at=None) == resolved
    assert resolved.bounds == {LAT: 150.0}


# ---- Task 2 rows -----------------------------------------------------------------------------

V = T1 + timedelta(hours=1)


def two_metric_scenario(*, change_jit: bool):
    fx = fuseki()
    lat = bind(fx, "i1", LAT, 100.0, IF0, T0)
    jit = bind(fx, "i1", JIT, 20.0, IF1, T0)
    if change_jit:
        replace_goal(fx, jit, 30.0, V - timedelta(seconds=10), T0)
    return fx, lat, jit


# ---- topology --------------------------------------------------------------------------------


def test_a_resolved_scope_is_eligible_even_with_zero_links():
    assert ce.topology_eligibility("resolved") == ClassEligibility(result="eligible", reasons=[])


def test_a_scope_entity_not_in_the_topology_is_a_gap():
    assert ce.topology_eligibility("missing") == ClassEligibility(
        result="gap", reasons=["scope entity not in the topology"])


def test_an_ambiguous_scope_entity_is_ineligible():
    assert ce.topology_eligibility("ambiguous") == ClassEligibility(
        result="ineligible", reasons=["ambiguous identity: scope entity"])


@pytest.mark.parametrize("flag,reason", [
    ({"conflicting_links": True}, "conflicting link records"),
    ({"colliding_ids": True}, "ambiguous identity: graph node"),
    ({"malformed": True}, "invalid topology record"),
])
def test_each_topology_flag_makes_a_resolved_scope_ineligible(flag, reason):
    assert ce.topology_eligibility("resolved", **flag) == ClassEligibility(
        result="ineligible", reasons=[reason])


def test_topology_flags_combine_and_beat_a_missing_scope():
    got = ce.topology_eligibility("missing", conflicting_links=True, malformed=True)
    assert got.result == "ineligible"
    assert got.reasons == sorted(["scope entity not in the topology", "conflicting link records",
                                  "invalid topology record"])


def test_an_unknown_scope_identity_is_refused():
    with pytest.raises(ValueError):
        ce.topology_eligibility("probably")


# ---- goals: the remaining rows ---------------------------------------------------------------


def test_a_placement_outside_the_authority_scope_is_ineligible():
    fx = fuseki()
    bind(fx, "i1", LAT, 100.0, IF0, T0)
    got = goals(fx, valid_at=V, allowed=lambda device: device != "olt1")
    assert got == ClassEligibility(result="ineligible",
                                   reasons=["placement outside authority scope: LatencyMetric"])


def test_the_authority_check_gets_the_device_name():
    fx = fuseki()
    bind(fx, "i1", LAT, 100.0, IF0, T0)
    seen = []
    goals(fx, valid_at=V, allowed=lambda device: seen.append(device) or True)
    assert seen == ["olt1"]


def test_a_metric_missing_from_the_metric_map_is_ineligible():
    fx = fuseki()
    bind(fx, "i1", LAT, 100.0, IF0, T0)
    got = goals(fx, valid_at=V, metric_map={})
    assert got == ClassEligibility(result="ineligible", reasons=["unmapped metric: LatencyMetric"])


def test_two_placements_inside_the_window_are_ineligible():
    fx = fuseki()
    binding = bind(fx, "i1", LAT, 100.0, IF0, T0)
    # A reissued placement that is a real change: the old one closed at the new start.
    import rdflib

    from data_contracts import fluents as fl
    from tests.fluent_helpers import UPDATE_URL
    at = V - timedelta(seconds=10)
    old = fl.fluent_iri(binding, "place", T0)
    g = rdflib.Graph()
    fl.add_placement(g, binding, IF1, at, at)
    fl.close(g, old, at)
    fx.post(UPDATE_URL, data=fl.guarded_insert(g, [old]).encode("utf-8"))
    got = goals(fx, valid_at=V)
    assert got == ClassEligibility(result="ineligible",
                                   reasons=["placement changed within window: LatencyMetric"])


def duplicate_fluents(kind, value_a, value_b):
    a = ce.Fluent("http://x/a", value_a, T0, None, T0, None)
    b = ce.Fluent("http://x/b", value_b, T0, None, T0, None)
    ok_goal = ce.Fluent("http://x/g", 1.0, T0, None, T0, None)
    ok_place = ce.Fluent("http://x/p", IF0, T0, None, T0, None)
    other = (LAT, "placement" if kind == "goal" else "goal")
    fl = {(LAT, kind): [a, b], other: [ok_place if kind == "goal" else ok_goal]}
    return fl


def test_two_distinct_goals_covering_the_valid_time_are_a_duplicate():
    got = ce.goals_eligibility(duplicate_fluents("goal", 1.0, 2.0), {IF0: ("olt1", "pon0")},
                               {LAT: MAPPING}, valid_at=V, known_at=None, limit_s=300,
                               allowed=ALLOW)
    assert got == ClassEligibility(result="ineligible",
                                   reasons=["duplicate goal in force: LatencyMetric"])


def test_two_distinct_placements_covering_the_valid_time_are_a_duplicate():
    got = ce.goals_eligibility(duplicate_fluents("placement", IF0, IF1),
                               {IF0: ("olt1", "pon0"), IF1: ("olt1", "pon1")},
                               {LAT: MAPPING}, valid_at=V, known_at=None, limit_s=300,
                               allowed=ALLOW)
    assert got == ClassEligibility(result="ineligible",
                                   reasons=["duplicate placement in force: LatencyMetric"])


def test_a_goal_with_no_placement_is_only_a_placement_gap():
    fl = {(LAT, "goal"): [ce.Fluent("http://x/g", 1.0, T0, None, T0, None)],
          (LAT, "placement"): []}
    got = ce.goals_eligibility(fl, {}, {LAT: MAPPING}, valid_at=V, known_at=None, limit_s=300,
                               allowed=ALLOW)
    assert got == ClassEligibility(result="gap", reasons=["no placement in force: LatencyMetric"])


def test_with_two_metrics_only_the_changed_one_is_named():
    fx, _lat, _jit = two_metric_scenario(change_jit=True)
    fluents, interfaces = read(fx)
    got = ce.goals_eligibility(fluents, interfaces,
                               {LAT: MAPPING, JIT: MetricMapping("jitter", JIT, "ms", "<")},
                               valid_at=V, known_at=None, limit_s=300, allowed=ALLOW)
    assert got == ClassEligibility(result="ineligible",
                                   reasons=["goal changed within window: JitterMetric"])


def test_two_unchanged_metrics_are_eligible():
    fx, _lat, _jit = two_metric_scenario(change_jit=False)
    fluents, interfaces = read(fx)
    got = ce.goals_eligibility(fluents, interfaces,
                               {LAT: MAPPING, JIT: MetricMapping("jitter", JIT, "ms", "<")},
                               valid_at=V, known_at=None, limit_s=300, allowed=ALLOW)
    assert got.result == "eligible"


# ---- the binding that could not be read ------------------------------------------------------


def test_no_binding_is_a_gap_and_an_invalid_binding_is_ineligible():
    assert ce.binding_problem("no binding for intent") == ClassEligibility(
        result="gap", reasons=["no binding for intent"])
    assert ce.binding_problem("invalid binding") == ClassEligibility(
        result="ineligible", reasons=["invalid binding"])


def test_any_other_binding_reason_is_refused():
    with pytest.raises(ValueError):
        ce.binding_problem("server said no")


# ---- observations ----------------------------------------------------------------------------

from data_contracts.envelope import ContextEnvelope, Quality, StateCategory  # noqa: E402


def env(*, age_s=10, value=5.0, stale=False, missing=False, conflicts=(), at=None):
    observed_at = at if at is not None else V - timedelta(seconds=age_s)
    return ContextEnvelope("olt1:pon0", StateCategory.OBSERVED, value, "victoriametrics",
                           None if missing else observed_at,
                           Quality(stale=stale, missing=missing, conflicts=list(conflicts)))


def obs(observed, metrics=(LAT,), limit_s=60):
    return ce.observations_eligibility(list(metrics), observed, valid_at=V, limit_s=limit_s)


def test_a_fresh_finite_single_series_sample_is_eligible():
    assert obs({LAT: env()}) == ClassEligibility(result="eligible", reasons=[])


def test_a_metric_not_observed_is_a_gap():
    got = obs({LAT: env()}, metrics=(LAT, JIT))
    assert got == ClassEligibility(result="gap", reasons=["no observation: JitterMetric"])


@pytest.mark.parametrize("envelope", [
    env(missing=True, value=None),
    env(value=None),
    env(value=math.nan),
    env(value=math.inf),
    env(value=-math.inf),
])
def test_a_missing_or_non_finite_sample_is_a_gap_never_eligible(envelope):
    assert obs({LAT: envelope}) == ClassEligibility(
        result="gap", reasons=["no observation: LatencyMetric"])


def test_conflicting_series_are_ineligible():
    got = obs({LAT: env(conflicts=["multiple series match selector (2)"])})
    assert got == ClassEligibility(result="ineligible",
                                   reasons=["conflicting observation: LatencyMetric"])


def test_a_conflict_reason_never_echoes_the_conflict_text():
    got = obs({LAT: env(conflicts=["<script>alert(1)</script> from server"])})
    assert "script" not in " ".join(got.reasons)


def test_the_stale_flag_is_ineligible():
    assert obs({LAT: env(stale=True)}) == ClassEligibility(
        result="ineligible", reasons=["stale observation: LatencyMetric"])


def test_a_sample_older_than_the_tightened_limit_is_stale_even_when_the_joiner_said_fresh():
    assert obs({LAT: env(age_s=120)}, limit_s=60) == ClassEligibility(
        result="ineligible", reasons=["stale observation: LatencyMetric"])
    assert obs({LAT: env(age_s=120)}, limit_s=300).result == "eligible"
    assert obs({LAT: env(age_s=60)}, limit_s=60).result == "eligible"  # exactly at the limit


def test_a_future_dated_sample_is_ineligible_beyond_the_tolerance():
    assert obs({LAT: env(at=V + timedelta(seconds=6))}) == ClassEligibility(
        result="ineligible", reasons=["future-dated observation: LatencyMetric"])
    assert obs({LAT: env(at=V + timedelta(seconds=5))}).result == "eligible"


def test_an_observed_sample_with_no_time_is_not_eligible():
    envelope = env()
    envelope.observed_at = None
    assert obs({LAT: envelope}).result != "eligible"


def test_one_failing_metric_beats_a_fine_one_and_each_is_judged_alone():
    got = obs({LAT: env(), JIT: env(stale=True)}, metrics=(LAT, JIT))
    assert got == ClassEligibility(result="ineligible",
                                   reasons=["stale observation: JitterMetric"])


# ---- overall ---------------------------------------------------------------------------------

ELIGIBLE = ClassEligibility(result="eligible", reasons=[])
GAP = ClassEligibility(result="gap", reasons=["no observation: LatencyMetric"])
BAD = ClassEligibility(result="ineligible", reasons=["stale observation: JitterMetric"])


def test_only_the_requested_classes_are_judged():
    got = ce.overall({"topology": ELIGIBLE})
    assert got.result == "eligible"
    assert list(got.classes) == ["topology"]
    assert got.reasons == []


def test_ineligible_beats_gap_beats_eligible():
    assert ce.overall({"topology": ELIGIBLE, "goals": GAP, "observations": BAD}).result == \
        "ineligible"
    assert ce.overall({"topology": ELIGIBLE, "observations": GAP}).result == "gap"
    assert ce.overall({"topology": ELIGIBLE, "goals": ELIGIBLE}).result == "eligible"


def test_overall_reasons_are_the_sorted_union_and_classes_keep_their_own():
    got = ce.overall({"observations": BAD, "goals": GAP})
    assert got.reasons == sorted(set(GAP.reasons) | set(BAD.reasons))
    assert got.classes["goals"].reasons == GAP.reasons
    assert got.classes["observations"].reasons == BAD.reasons


def test_overall_of_no_classes_is_eligible():
    assert ce.overall({}).result == "eligible"


# ---- reasons are fixed templates -------------------------------------------------------------

ADVERSARIAL = 'http://x/y#Late ncy"; <script>alert(1)</script> {}'
SAFE_AFTER_COLON = re.compile(r"^[A-Za-z0-9_.\-]+$")
TEMPLATES = re.compile(
    r"^(no binding for intent|invalid binding|scope entity not in the topology"
    r"|ambiguous identity: scope entity|ambiguous identity: graph node"
    r"|conflicting link records|invalid topology record"
    r"|no events in window|window reaches beyond event retention|ambiguous identity: event source"
    r"|(no goal in force|no placement in force|duplicate goal in force"
    r"|duplicate placement in force|goal changed within window|placement changed within window"
    r"|placement outside authority scope|unmapped metric|no observation"
    r"|conflicting observation|stale observation|future-dated observation): ([^ ]+))$")


def check_fixed(reasons):
    for reason in reasons:
        m = TEMPLATES.match(reason)
        assert m, reason
        if m.group(3):
            assert SAFE_AFTER_COLON.match(m.group(3)), reason


def test_adversarial_metric_names_only_ever_vary_the_short_name():
    # One fluent set per kind of goals reason, all for a hostile metric URI.
    ok_p = ce.Fluent("http://x/p", IF0, T0, None, T0, None)
    ok_g = ce.Fluent("http://x/g", 1.0, T0, None, T0, None)
    dup_g = [ok_g, ce.Fluent("http://x/g2", 2.0, T0, None, T0, None)]
    dup_p = [ok_p, ce.Fluent("http://x/p2", IF1, T0, None, T0, None)]
    ifaces = {IF0: ("olt1", "pon0"), IF1: ("olt1", "pon1")}
    produced = []
    for fl in (
        {(ADVERSARIAL, "goal"): dup_g, (ADVERSARIAL, "placement"): dup_p},
        {(ADVERSARIAL, "goal"): [], (ADVERSARIAL, "placement"): []},
        {(ADVERSARIAL, "goal"): [ok_g], (ADVERSARIAL, "placement"): [ok_p]},
    ):
        for allowed in (ALLOW, lambda d: False):
            for mm in ({}, {ADVERSARIAL: MAPPING}):
                produced += ce.goals_eligibility(fl, ifaces, mm, valid_at=V, known_at=None,
                                                 limit_s=300, allowed=allowed).reasons
    changed = ce.Fluent("http://x/old", 1.0, T0, V - timedelta(seconds=5), T0, None)
    new = ce.Fluent("http://x/new", 2.0, V - timedelta(seconds=5), None, T0, None)
    produced += ce.goals_eligibility(
        {(ADVERSARIAL, "goal"): [changed, new], (ADVERSARIAL, "placement"): [ok_p]}, ifaces,
        {ADVERSARIAL: MAPPING}, valid_at=V, known_at=None, limit_s=300, allowed=ALLOW).reasons
    bad = env(stale=True, conflicts=["<script>"])
    produced += obs({ADVERSARIAL: bad}, metrics=(ADVERSARIAL, ADVERSARIAL + "2")).reasons
    produced += obs({ADVERSARIAL: env(value=math.nan)}, metrics=(ADVERSARIAL,)).reasons
    produced += obs({ADVERSARIAL: env(at=V + timedelta(minutes=5))}, metrics=(ADVERSARIAL,)).reasons
    assert produced
    check_fixed(produced)
    assert all(not set('"<>{};') & set(r) for r in produced)


def test_every_reason_of_the_other_classes_is_a_template():
    reasons = []
    for scope in ("resolved", "missing", "ambiguous"):
        reasons += ce.topology_eligibility(scope, conflicting_links=True, colliding_ids=True,
                                           malformed=True).reasons
    reasons += ce.binding_problem("no binding for intent").reasons
    reasons += ce.binding_problem("invalid binding").reasons
    check_fixed(reasons)


# ---- apart from the verdict ------------------------------------------------------------------

FORBIDDEN = ("intent_management", "outcome_assurance.evaluate", "broker_engine.assess",
             "broker_engine.temporal")


def imported(path):
    tree = ast.parse(Path(path).read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.append((node.module, [a.name for a in node.names]))
        elif isinstance(node, ast.Import):
            found += [(a.name, []) for a in node.names]
    return found


def test_eligibility_imports_nothing_from_the_verdict_path():
    found = imported(ce.__file__)
    assert [m for m, _ in found if m and m.startswith(FORBIDDEN)] == []
    from_report = [names for m, names in found if m == "outcome_assurance.tmf921_report"]
    assert from_report == [["short_name"]]


def test_eligibility_neither_takes_nor_returns_a_verdict():
    fx, _lat, _jit = two_metric_scenario(change_jit=False)
    got = goals(fx, valid_at=V)
    assert "verdict" not in got.model_dump()
    assert not any(word in " ".join(ce.__doc__.split()) for word in ("success|failure",))


# ---- the events class ------------------------------------------------------------------------

@pytest.mark.parametrize("args,result,reasons", [
    ((0, 0, False), "gap", ["no events in window"]),
    ((3, 0, False), "eligible", []),
    ((3, 1, False), "ineligible", ["ambiguous identity: event source"]),
    ((0, 0, True), "gap", ["no events in window", "window reaches beyond event retention"]),
    ((3, 0, True), "gap", ["window reaches beyond event retention"]),
    ((3, 1, True), "ineligible", ["ambiguous identity: event source",
                                  "window reaches beyond event retention"]),
])
def test_events_eligibility_results(args, result, reasons):
    count, ambiguous, beyond = args
    got = ce.events_eligibility(count=count, ambiguous=ambiguous, beyond_retention=beyond)
    assert (got.result, got.reasons) == (result, reasons)


def test_events_eligibility_takes_no_conflict_or_verdict_argument():
    import inspect
    assert sorted(inspect.signature(ce.events_eligibility).parameters) == [
        "ambiguous", "beyond_retention", "count"]


def test_the_events_reasons_are_templates():
    reasons = []
    for args in ((0, 0, True), (3, 1, True)):
        reasons += ce.events_eligibility(count=args[0], ambiguous=args[1], beyond_retention=args[2]).reasons
    check_fixed(reasons)


def test_no_broker_engine_module_imports_the_syslog_parser_or_ingest():
    root = Path(ce.__file__).parent
    offenders = []
    for path in sorted(root.glob("**/*.py")):
        for module, names in imported(path):
            joined = [module or ""] + [f"{module}.{n}" for n in names]
            if any(j.startswith(("ingestion.syslog_parser", "ingestion.syslog_ingest")) for j in joined):
                offenders.append(path.name)
    assert offenders == []


def test_the_contract_module_imports_nothing_from_ingestion():
    from data_contracts import context_contract
    assert [m for m, _ in imported(context_contract.__file__) if m and m.startswith("ingestion")] == []


# ---- knowledge ----------------------------------------------------------------------------------


@pytest.mark.parametrize("outcome,applicable,result,reasons", [
    ("unavailable", 0, "gap", ["knowledge retrieval unavailable"]),
    ("not_current", 0, "gap", ["knowledge is current only"]),
    ("answered", 0, "gap", ["no applicable knowledge"]),
    ("answered", 2, "eligible", []),
])
def test_knowledge_eligibility_rows(outcome, applicable, result, reasons):
    assert ce.knowledge_eligibility(outcome=outcome, applicable=applicable) == ClassEligibility(
        result=result, reasons=reasons)


def test_an_unknown_retrieval_outcome_is_refused():
    with pytest.raises(ValueError):
        ce.knowledge_eligibility(outcome="probably", applicable=1)


def test_knowledge_eligibility_takes_only_the_outcome_and_the_count():
    import inspect
    assert sorted(inspect.signature(ce.knowledge_eligibility).parameters) == ["applicable", "outcome"]
