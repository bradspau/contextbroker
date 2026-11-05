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
