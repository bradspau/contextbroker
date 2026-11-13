"""The closed neighbour-discovery formats and the pure hypothesis rule (plan 07-07, SEM-01, D-62, D-65).

The owner approved three formats at the 07-07 checkpoint (answer approve-all). Each is anchored,
case-sensitive and closed, so every near miss below must read as no adjacency at all.
"""
import ast
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from data_contracts import adjacency
from data_contracts import syslog_events
from data_contracts.adjacency import ADJACENCY_FORMATS, Adjacency, recognise_adjacency
from data_contracts.promotion import PROMOTED_SUPPORT_MAX, RULE_ADJACENCY, promoted_id
from state_reconciliation import promotion
from state_reconciliation.promotion import (
    AdjacencyObservation, PromotedRecord, select_hypotheses)

L, P, R = "GigabitEthernet0/0/1", "Router-C", "GigabitEthernet0/0/2"
F1 = ("%CDP-4-DUPLEX_MISMATCH: duplex mismatch discovered on {l} (not half duplex), "
      "with {p} {r} (half duplex).")
F2 = "%CDP-4-NATIVE_VLAN_MISMATCH: Native VLAN mismatch discovered on {l} (1), with {p} {r} (10)."
F3 = ("LLDP_NEIGHBOR_UP: LLDP neighbor up on local port {l}, remote system {p}, "
      "remote port {r}")
TAGS = {F1: "%CDP-4-DUPLEX_MISMATCH", F2: "%CDP-4-NATIVE_VLAN_MISMATCH", F3: "LLDP_NEIGHBOR_UP"}
TEMPLATES = (F1, F2, F3)
EXPECTED = Adjacency(L, P, R)


def say(template, l=L, p=P, r=R):
    return template.format(l=l, p=p, r=r)


# ---- the approved set ----

def test_the_approved_names_are_the_three_in_the_order_approved():
    assert ADJACENCY_FORMATS == ("cdp_duplex_mismatch", "cdp_native_vlan_mismatch",
                                 "lldp_neighbor_up")


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_example_line_gives_the_adjacency(template):
    assert recognise_adjacency(say(template)) == EXPECTED


def test_each_cdp_form_reads_every_duplex_and_the_optional_full_stop():
    for local in ("full", "half", "not full", "not half"):
        for remote in ("full", "half"):
            msg = (f"%CDP-4-DUPLEX_MISMATCH: duplex mismatch discovered on {L} ({local} duplex), "
                   f"with {P} {R} ({remote} duplex)")
            assert recognise_adjacency(msg) == EXPECTED
            assert recognise_adjacency(msg + ".") == EXPECTED
    assert recognise_adjacency(say(F2).rstrip(".")) == EXPECTED
    assert recognise_adjacency(say(F2).replace("(1)", "(1234)")) == EXPECTED


@pytest.mark.parametrize("template", TEMPLATES)
def test_a_cisco_sequence_prefix_and_a_leading_byte_order_mark_are_ignored(template):
    line = say(template)
    assert recognise_adjacency("000123: *Nov  6 11:55:00.123: " + line) == EXPECTED
    assert recognise_adjacency("\ufeff" + line) == EXPECTED


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_longest_names_still_match(template):
    assert recognise_adjacency(say(template, l="G" * 64, p="P" * 128, r="g" * 64)) == Adjacency(
        "G" * 64, "P" * 128, "g" * 64)


# ---- near misses: each gives None ----

@pytest.mark.parametrize("template", TEMPLATES)
def test_near_miss_lower_case_tag_or_a_letter_before_it(template):
    tag = TAGS[template]
    assert recognise_adjacency(say(template).replace(tag, tag.lower())) is None
    assert recognise_adjacency("x" + say(template)) is None
    assert recognise_adjacency("7" + say(template)) is None


@pytest.mark.parametrize("template,changed", [
    (F1, ("-4-", "-5-")), (F2, ("-4-", "-5-")), (F3, ("NEIGHBOR_UP", "NEIGHBOR_DOWN"))])
def test_near_miss_a_different_severity_or_event_name(template, changed):
    assert recognise_adjacency(say(template).replace(*changed)) is None


@pytest.mark.parametrize("template", TEMPLATES)
def test_near_miss_text_after_the_end_or_a_second_line(template):
    assert recognise_adjacency(say(template) + " extra") is None
    assert recognise_adjacency(say(template) + "\n") is None
    if template is not F3:  # the last name of F3 may itself hold full stops
        assert recognise_adjacency(say(template) + "..") is None


@pytest.mark.parametrize("template", TEMPLATES)
def test_near_miss_names_over_their_length_limits(template):
    assert recognise_adjacency(say(template, l="G" * 65)) is None
    assert recognise_adjacency(say(template, r="G" * 65)) is None
    assert recognise_adjacency(say(template, p="P" * 129)) is None


@pytest.mark.parametrize("template", TEMPLATES)
@pytest.mark.parametrize("peer", ["Router C", 'Rou"ter', "Rou;ter", "Rou(ter", "Rou,ter", "", "R/C"])
def test_near_miss_a_peer_outside_its_character_class(template, peer):
    assert recognise_adjacency(say(template, p=peer)) is None


@pytest.mark.parametrize("template", TEMPLATES)
@pytest.mark.parametrize("name", ["", "Gi 0/1", 'Gi"0', "Gi;0", "Gi(0)", "Gi,0", "Gi\u00e90"])
def test_near_miss_an_interface_outside_its_character_class(template, name):
    assert recognise_adjacency(say(template, l=name)) is None
    assert recognise_adjacency(say(template, r=name)) is None


@pytest.mark.parametrize("template,message", [
    (F1, "%CDP-4-DUPLEX_MISMATCH: duplex mismatch discovered on Gi0/0/1 (half duplex), "
         "with Router-C (half duplex)."),
    (F2, "%CDP-4-NATIVE_VLAN_MISMATCH: Native VLAN mismatch discovered on Gi0/0/1 (1), "
         "with Router-C (10)."),
    (F3, "LLDP_NEIGHBOR_UP: LLDP neighbor up on local port Gi0/0/1, remote system Router-C, "
         "remote port "),
])
def test_near_miss_a_missing_peer_interface(template, message):
    assert recognise_adjacency(message) is None


def test_near_miss_a_vlan_or_duplex_outside_its_fixed_shape():
    assert recognise_adjacency(say(F2).replace("(1)", "(12345)")) is None
    assert recognise_adjacency(say(F2).replace("(1)", "(x)")) is None
    assert recognise_adjacency(say(F2).replace("(1)", "(\u0661)")) is None  # a non-ASCII digit
    assert recognise_adjacency(say(F1).replace("half duplex).", "quarter duplex).")) is None
    assert recognise_adjacency(say(F1).replace("(not half", "(never half")) is None


@pytest.mark.parametrize("template", TEMPLATES)
def test_near_miss_text_crossed_from_another_approved_format(template):
    for other in TEMPLATES:
        if other is not template:
            crossed = say(template).replace(TAGS[template], TAGS[other])
            assert recognise_adjacency(crossed) is None


def test_near_miss_unrelated_text_gives_none():
    for text in ("", " ", "%LINK-3-UPDOWN: Interface Gi0/0/1, changed state to down",
                 "neighbor Router-C on Gi0/0/2", None, 5, b"x"):
        assert recognise_adjacency(text) is None


def test_a_long_hostile_line_is_answered_without_backtracking_trouble():
    hostile = TAGS[F1] + ": duplex mismatch discovered on " + "a/" * 2000
    assert recognise_adjacency(hostile) is None
    assert recognise_adjacency("%CDP-4-" * 600) is None


# ---- the module itself ----

def test_the_interface_class_equals_the_one_in_the_event_kind_table():
    assert adjacency._IFACE == syslog_events._IFACE


def test_an_adjacency_is_frozen():
    with pytest.raises(FrozenInstanceError):
        EXPECTED.peer = "x"


def _roots(path):
    roots = set()
    for node in ast.walk(ast.parse(Path(path).read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            roots.add(node.module.split(".")[0])
    return roots


def test_the_format_module_imports_only_re_and_dataclasses():
    assert _roots(adjacency.__file__) == {"re", "dataclasses"}


def test_the_rule_module_still_imports_only_the_standard_library_and_pure_packages():
    roots = _roots(promotion.__file__)
    assert roots <= {"data_contracts", "state_reconciliation"} | set(sys.stdlib_module_names), roots


# ---- select_hypotheses (pure) ----

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
A, C = "Router-A/GigabitEthernet0/0/1", "Router-C/GigabitEthernet0/0/2"
KNOWN = frozenset({A, C, "Router-A/GigabitEthernet0/0/0", "Router-B/GigabitEthernet0/0/1"})
ID = promoted_id("hypothesis", RULE_ADJACENCY, (A, C))


def s(n):
    return timedelta(seconds=n)


def obs(n, at=100, local=A, peer=C, event=None):
    return AdjacencyObservation(event or "evt-" + f"{n:032x}", local, peer, NOW - s(at))


def pick(observations, *, known=KNOWN, linked=frozenset(), min_events=3, window_s=900, now=NOW):
    return select_hypotheses(observations, known_interfaces=known, linked_interfaces=linked,
                             min_events=min_events, window_s=window_s, now=now)


THREE = [obs(1, 300), obs(2, 200), obs(3, 100)]
THREE_IDS = tuple("evt-" + f"{n:032x}" for n in (1, 2, 3))


def test_below_the_threshold_selects_nothing_and_at_it_selects_one():
    assert pick(THREE[:2]) == ()
    [record] = pick(THREE)
    assert isinstance(record, PromotedRecord)
    assert (record.kind, record.rule) == ("hypothesis", RULE_ADJACENCY)
    assert record.promoted_id == ID == promoted_id("hypothesis", RULE_ADJACENCY, tuple(sorted((A, C))))
    assert (record.device_a, record.device_b) == ("Router-A", "Router-C")
    assert record.body == {"from": A, "to": C}
    assert record.valid_from == NOW - s(300)
    assert record.event_ids == tuple("evt-" + f"{n:032x}" for n in (1, 2, 3))


def test_the_window_is_inclusive_and_wider_spread_selects_nothing():
    assert len(pick(THREE, window_s=200)) == 1
    assert pick(THREE, window_s=199) == ()


def test_the_pair_is_the_same_whichever_end_the_device_wrote_first():
    flipped = [obs(1, 300, local=C, peer=A), obs(2, 200), obs(3, 100, local=C, peer=A)]
    [record] = pick(flipped)
    assert record.body == {"from": A, "to": C} and record.promoted_id == ID


def test_an_end_that_is_not_known_selects_nothing():
    assert pick(THREE, known=KNOWN - {C}) == ()
    assert pick(THREE, known=KNOWN - {A}) == ()


def test_an_end_with_a_link_in_force_selects_nothing():
    assert pick(THREE, linked=frozenset({A})) == ()
    assert pick(THREE, linked=frozenset({C})) == ()
    assert len(pick(THREE, linked=frozenset({"Router-B/GigabitEthernet0/0/1"}))) == 1


def test_a_local_node_equal_to_the_peer_node_selects_nothing():
    same = [obs(n, 400 - 100 * n, peer=A) for n in (1, 2, 3)]
    assert pick(same) == ()


def test_a_repeated_event_id_counts_once():
    assert pick([obs(1, 300), obs(1, 200, event="evt-" + f"{1:032x}"), obs(3, 100)]) == ()
    repeated = THREE + [obs(1, 300)]
    assert pick(repeated)[0].event_ids == THREE_IDS


def test_the_same_input_twice_gives_an_identical_record_and_the_input_is_not_changed():
    given = list(reversed(THREE))
    snapshot = list(given)
    assert pick(given) == pick(given) == pick(THREE)
    assert given == snapshot


def test_records_are_sorted_by_promoted_id_and_pairs_are_kept_apart():
    other = "Router-B/GigabitEthernet0/0/1"
    more = [obs(n + 10, 400 - 100 * n, local="Router-A/GigabitEthernet0/0/0", peer=other)
            for n in (1, 2, 3)]
    records = pick(THREE + more)
    assert len(records) == 2
    assert [r.promoted_id for r in records] == sorted(r.promoted_id for r in records)


def test_valid_from_is_the_first_event_of_the_first_window_never_after_now():
    early = [obs(0, 5000)] + THREE
    assert pick(early, window_s=250)[0].valid_from == NOW - s(300)
    future = [obs(n, -(10 * n)) for n in (1, 2, 3)]  # stamped after now
    assert pick(future)[0].valid_from == NOW


def test_support_is_capped_in_placed_order():
    many = [obs(n, 400 - n) for n in range(1, PROMOTED_SUPPORT_MAX + 6)]
    [record] = pick(many)
    assert len(record.event_ids) == PROMOTED_SUPPORT_MAX
    assert record.event_ids == tuple("evt-" + f"{n:032x}" for n in range(1, PROMOTED_SUPPORT_MAX + 1))


def test_support_is_the_events_of_the_qualifying_window_not_the_oldest_events():
    older = [obs(n, 100000 - 2000 * n) for n in range(1, 26)]  # 25 events, 2000 s apart
    burst = [obs(100 + k, 300 - 100 * k) for k in range(3)]
    [record] = pick(older + burst)
    assert record.event_ids == tuple("evt-" + f"{100 + k:032x}" for k in range(3))


def test_a_naive_now_or_a_bad_threshold_raises_and_a_naive_observation_is_refused():
    with pytest.raises(ValueError):
        pick(THREE, now=datetime(2026, 11, 6, 12, 0, 0))
    with pytest.raises(ValueError):
        pick(THREE, min_events=0)
    with pytest.raises(ValueError):
        pick(THREE, window_s=True)
    with pytest.raises(ValueError):
        AdjacencyObservation("evt-1", A, C, datetime(2026, 11, 6, 12, 0, 0))
    with pytest.raises(FrozenInstanceError):
        THREE[0].local_node = C
    assert replace(THREE[0], local_node=C).local_node == C


def test_no_observations_select_nothing():
    assert pick([]) == ()
