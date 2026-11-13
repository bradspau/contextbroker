"""The conflict rule: pure, closed, and selecting exactly at its threshold and window (SEM-01, D-56, D-57).

Plan 07-04. Thresholds are inputs; nothing a caller or a device sends can move them.
"""
import ast
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from data_contracts import promotion as vocabulary
from data_contracts.promotion import PROMOTED_SUPPORT_MAX, RULE_REPEATED_LINK_DOWN, promoted_id
from state_reconciliation import promotion
from state_reconciliation.event_reconciler import CONFIGURED_LINK_IN_FORCE, ConfiguredSide
from state_reconciliation.promotion import (
    CountedEvent, PromotedRecord, first_window, select_conflicts)

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
T0 = NOW - timedelta(seconds=20000)
HELD = ConfiguredSide("Router-A", CONFIGURED_LINK_IN_FORCE)
TEXT = "event link_down contradicts configured link_in_force"


def s(n):
    return timedelta(seconds=n)


def eid(n):
    return "evt-" + f"{n:032x}"


def down(n, at=None, device="Router-A", kind="link_down"):
    return CountedEvent(eid(n), device, kind, T0 + s(n * 100) if at is None else at)


def pick(events, configured=HELD, **kw):
    args = dict(device="Router-A", configured=configured, min_events=3, window_s=900, now=NOW)
    args.update(kw)
    return select_conflicts(events, **args)


# ---- the support comes from the qualifying window (bead contextbroker-bnl) ----

def test_support_is_the_events_of_the_qualifying_window_not_the_oldest_events():
    older = [down(n, at=T0 + s(n * 500)) for n in range(1, 26)]  # 25 events, no three within 900 s
    burst_start = T0 + s(15000)
    burst = [down(100 + k, at=burst_start + s(k * 100)) for k in range(3)]
    [record] = pick(older + burst)
    assert record.event_ids == tuple(eid(100 + k) for k in range(3))
    assert record.valid_from == burst_start


def test_support_stops_at_the_end_of_the_window():
    inside = [down(n, at=T0 + s(n * 100)) for n in range(1, 5)]  # 100..400 s
    after = [down(50, at=T0 + s(5000)), down(51, at=T0 + s(5100))]
    [record] = pick(inside + after, min_events=3, window_s=450)
    assert record.event_ids == tuple(eid(n) for n in range(1, 5))


def test_a_threshold_above_the_support_cap_lists_the_first_cap_events_of_the_window():
    many = [down(n, at=T0 + s(n)) for n in range(1, PROMOTED_SUPPORT_MAX + 11)]
    [record] = pick(many, min_events=PROMOTED_SUPPORT_MAX + 5, window_s=900)
    assert record.event_ids == tuple(eid(n) for n in range(1, PROMOTED_SUPPORT_MAX + 1))


# ---- first_window ----

def test_first_window_is_inclusive_at_the_window_edge():
    t = [T0, T0 + s(450), T0 + s(900)]
    assert first_window(t, 3, 900) == 0
    assert first_window([T0, T0 + s(450), T0 + s(901)], 3, 900) is None


def test_first_window_finds_the_lowest_index_that_fits():
    t = [T0, T0 + s(5000), T0 + s(5100), T0 + s(5200)]
    assert first_window(t, 3, 900) == 1


def test_first_window_with_too_few_times_is_none():
    assert first_window([T0, T0 + s(1)], 3, 900) is None
    assert first_window([], 1, 1) is None


def test_first_window_leaves_its_input_alone():
    t = [T0, T0 + s(1), T0 + s(2)]
    before = list(t)
    first_window(t, 3, 900)
    assert t == before


@pytest.mark.parametrize("min_events,window_s", [(0, 900), (3, 0), (True, 900), (3, 1.5), ("3", 900)])
def test_first_window_refuses_a_bad_threshold(min_events, window_s):
    with pytest.raises(ValueError):
        first_window([T0], min_events, window_s)


# ---- select_conflicts: the threshold ----

def test_threshold_two_events_select_nothing_and_three_select_one_record():
    assert pick([down(1), down(2)]) == ()
    [record] = pick([down(1), down(2), down(3)])
    assert isinstance(record, PromotedRecord)
    assert record.kind == "conflict" and record.rule == RULE_REPEATED_LINK_DOWN
    assert (record.device_a, record.device_b) == ("Router-A", None)
    assert record.body == {"device": "Router-A", "event": "link_down",
                           "configured": "link_in_force", "text": TEXT}
    assert record.event_ids == (eid(1), eid(2), eid(3))


def test_threshold_follows_the_argument_not_a_constant():
    assert pick([down(1), down(2)], min_events=2) != ()
    assert pick([down(1), down(2), down(3)], min_events=4) == ()


def test_window_events_further_apart_than_the_window_select_nothing():
    spread = [down(1, T0), down(2, T0 + s(1000)), down(3, T0 + s(2000))]
    assert pick(spread) == ()
    assert pick(spread, window_s=2000) != ()


def test_window_a_dense_run_later_in_the_list_still_selects():
    events = [down(1, T0), down(2, T0 + s(5000)), down(3, T0 + s(5100)), down(4, T0 + s(5200))]
    [record] = pick(events)
    assert record.valid_from == T0 + s(5000)


# ---- select_conflicts: what is not counted ----

def test_a_link_up_event_is_not_counted():
    assert pick([down(1), down(2), down(3, kind="link_up")]) == ()


def test_an_event_of_another_device_is_not_counted():
    assert pick([down(1), down(2), down(3, device="Router-B")]) == ()


def test_an_unrecognised_event_is_not_counted():
    assert pick([down(1), down(2), down(3, kind=None)]) == ()


def test_a_repeated_event_id_counts_once():
    assert pick([down(1), down(1), down(2)]) == ()
    assert pick([down(1), down(1), down(2), down(3)]) != ()


def test_no_configured_link_selects_nothing():
    events = [down(1), down(2), down(3)]
    assert pick(events, configured=ConfiguredSide("Router-A", None)) == ()


def test_a_configured_side_for_another_device_selects_nothing():
    events = [down(1), down(2), down(3)]
    assert pick(events, configured=ConfiguredSide("Router-B", CONFIGURED_LINK_IN_FORCE)) == ()


# ---- select_conflicts: support and valid_from ----

def test_support_holds_the_first_twenty_by_time_then_id():
    events = [down(n, T0 + s(n)) for n in range(25, 0, -1)]
    [record] = pick(events)
    assert len(record.event_ids) == PROMOTED_SUPPORT_MAX == 20
    assert record.event_ids == tuple(eid(n) for n in range(1, 21))


def test_equal_times_are_ordered_by_event_id():
    events = [down(n, T0) for n in (3, 1, 2)]
    [record] = pick(events)
    assert record.event_ids == (eid(1), eid(2), eid(3))


def test_valid_from_is_the_first_event_of_the_first_qualifying_window():
    [record] = pick([down(3), down(1), down(2)])
    assert record.valid_from == T0 + s(100)


def test_valid_from_is_clamped_to_now_for_a_future_device_time():
    future = NOW + s(10000)
    events = [down(n, future + s(n)) for n in (1, 2, 3)]
    [record] = pick(events)
    assert record.valid_from == NOW


def test_the_same_input_gives_an_identical_record_twice_with_the_pinned_id():
    events = [down(1), down(2), down(3)]
    assert pick(events) == pick(list(reversed(events)))
    [record] = pick(events)
    assert record.promoted_id == promoted_id("conflict", RULE_REPEATED_LINK_DOWN, ("Router-A",))


def test_the_inputs_are_not_changed():
    events = [down(3), down(1), down(2)]
    before = list(events)
    pick(events)
    assert events == before and HELD == ConfiguredSide("Router-A", CONFIGURED_LINK_IN_FORCE)


# ---- bad arguments ----

def test_a_naive_now_is_refused():
    with pytest.raises(ValueError):
        pick([down(1), down(2), down(3)], now=datetime(2026, 11, 6, 12, 0, 0))


@pytest.mark.parametrize("kw", [{"min_events": 0}, {"window_s": 0}])
def test_a_threshold_below_one_is_refused(kw):
    with pytest.raises(ValueError):
        pick([down(1), down(2), down(3)], **kw)


def test_a_counted_event_checks_its_kind_and_its_time():
    with pytest.raises(ValueError):
        CountedEvent(eid(1), "Router-A", "link_sideways", T0)
    with pytest.raises(ValueError):
        CountedEvent(eid(1), "Router-A", "link_down", datetime(2026, 11, 6, 12, 0, 0))
    with pytest.raises(FrozenInstanceError):
        down(1).kind = "link_up"
    assert replace(down(1), kind=None).kind is None


# ---- purity ----

def _roots(path):
    roots = set()
    for node in ast.walk(ast.parse(Path(path).read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            roots.add(node.module.split(".")[0])
    return roots


def test_the_rule_module_imports_only_the_standard_library_and_pure_packages():
    roots = _roots(promotion.__file__)
    assert roots <= {"data_contracts", "state_reconciliation"} | set(sys.stdlib_module_names), roots
    for forbidden in ("intent_management", "outcome_assurance", "governance_gate", "action_lifecycle",
                      "broker_engine", "broker_state", "ingestion"):
        assert forbidden not in roots


def test_the_rule_module_reads_no_clock():
    source = Path(promotion.__file__).read_text()
    for call in ("datetime.now", "time.time", "date.today", "utcnow"):
        assert call not in source


def test_the_vocabulary_module_imports_only_the_standard_library():
    assert _roots(vocabulary.__file__) <= set(sys.stdlib_module_names)
