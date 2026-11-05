"""The shared event vocabulary: the event id and its module boundary."""
import ast
import hashlib
import time
from pathlib import Path

import pytest

from data_contracts import syslog_events
from data_contracts.syslog_events import EVENT_ID_PATTERN, EVENT_KINDS, event_id, recognise

STDLIB_ONLY = {"__future__", "dataclasses", "datetime", "hashlib", "re", "typing"}


def test_event_id_is_evt_plus_32_hex_of_sha256_over_the_raw_line():
    raw = "<13>1 - h a - - - x"
    assert event_id(raw) == "evt-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def test_event_id_is_stable_and_sensitive_to_one_character():
    a = "<13>1 - h a - - - x"
    assert event_id(a) == event_id(a)
    assert event_id(a) != event_id(a[:-1] + "y")


def test_event_id_matches_its_pattern_and_not_with_a_trailing_newline():
    i = event_id("<13>1 - h a - - - x")
    assert EVENT_ID_PATTERN.match(i)
    assert not EVENT_ID_PATTERN.match(i + "\n")
    assert not EVENT_ID_PATTERN.match(i.upper())


def test_module_imports_only_the_standard_library():
    tree = ast.parse(Path(syslog_events.__file__).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            roots.add(node.module.split(".")[0])
    assert roots <= STDLIB_ONLY, roots


IFACE = "GigabitEthernet0/0/0"

RECOGNISED = [
    (f"%LINK-3-UPDOWN: Interface {IFACE}, changed state to down", "link_down"),
    (f"%LINK-3-UPDOWN: Interface {IFACE}, changed state to up", "link_up"),
    (f"%LINEPROTO-5-UPDOWN: Line protocol on Interface {IFACE}, changed state to down", "link_down"),
    (f"%LINEPROTO-5-UPDOWN: Line protocol on Interface {IFACE}, changed state to up", "link_up"),
    ("123: *Oct  6 10:00:00.123: %LINK-3-UPDOWN: Interface Gi0/1, changed state to down", "link_down"),
    ("Oct  6 10:00:00: %LINK-3-UPDOWN: Interface Gi0/1.100, changed state to up", "link_up"),
    ("%LINK-3-UPDOWN: Interface Port-channel1:2_a, changed state to up", "link_up"),
    ("%LINK-3-UPDOWN: Interface " + "G" * 64 + ", changed state to down", "link_down"),
    ("NIC Link is Down", "link_down"),
    ("NIC Link is Up", "link_up"),
    ("e1000e: eth0 NIC Link is Up 1000 Mbps Full Duplex", "link_up"),
    ("igb 0000:01:00.0 eth1: igb: eth1 NIC Link is Down", "link_down"),
    ("\ufeff%LINK-3-UPDOWN: Interface Gi0/1, changed state to down", "link_down"),
]

NEAR_MISSES = [
    "",
    "link down",
    "The link down alarm was cleared on Gi0/1",
    "interface Gi0/1 is down",
    "Gi0/1 changed state to down",
    "%LINK-3-UPDOWN: Interface Gi0/1, changed state to administratively down",
    "%LINK-3-UPDOWN: Interface Gi0/1, changed state to down now",
    "%LINK-3-UPDOWN: Interface Gi0/1, changed state to down ",
    "%LINK-3-UPDOWN: Interface Gi0/1, changed state to Down",
    "%LINK-3-UPDOWN: Interface Gi0/1, changed state to upgraded",
    "%link-3-updown: Interface Gi0/1, changed state to down",
    "%LINK-3-UPDOWN: interface Gi0/1, changed state to down",
    "%LINK-8-UPDOWN: Interface Gi0/1, changed state to down",
    "%LINK-3-UPDOWN: Interface , changed state to down",
    "%LINK-3-UPDOWN: Interface Gi 0/1, changed state to down",
    "%LINK-3-UPDOWN: Interface " + "G" * 65 + ", changed state to down",
    "X%LINK-3-UPDOWN: Interface Gi0/1, changed state to down",
    "%LINEPROTO-5-UPDOWN: Line protocol on Interface Gi0/1, changed state to down!",
    "%LINEPROTO-5-UPDOWN: Interface Gi0/1, changed state to down",
    "NIC Link is Downgraded",
    "NIC Link is Upgraded",
    "NIC Link is Down1",
    "NIC Link is down",
    "NIC link is Down",
    "xNIC Link is Down",
    "NIC Link is",
]


@pytest.mark.parametrize("message, kind", RECOGNISED)
def test_recognise_known_forms(message, kind):
    assert recognise(message) == kind


@pytest.mark.parametrize("message", NEAR_MISSES)
def test_recognise_returns_none_for_near_misses(message):
    assert recognise(message) is None


def test_event_kinds_is_exactly_link_down_and_link_up():
    assert EVENT_KINDS == ("link_down", "link_up")
    for message, _ in RECOGNISED:
        assert recognise(message) in EVENT_KINDS
    for message in NEAR_MISSES:
        assert recognise(message) in (None, *EVENT_KINDS)


def test_recognise_is_fast_on_hostile_text_at_the_line_cap():
    for text in ("%LINK-3-UPDOWN: Interface " + "a" * 4000, "NIC Link is " * 300, "%" * 4000, "NIC " * 1000):
        started = time.perf_counter()
        assert recognise(text) is None
        assert time.perf_counter() - started < 0.5
