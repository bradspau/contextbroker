"""Strict syslog parser: a line is a ParsedLine or a Quarantined reason (D-20, D-21)."""
from datetime import datetime, timedelta, timezone

import pytest

from data_contracts.syslog_events import ParsedLine
from ingestion.syslog_parser import map_host, parse_line

LINE_5424 = "<34>1 2026-10-26T10:00:00Z Router-A sshd 123 ID47 - Failed login"


def p5424(rest="2026-10-26T10:00:00Z Router-A sshd 123 ID47 - msg", pri="<13>"):
    return pri + "1 " + rest


# --- RFC 5424 ---------------------------------------------------------------


def test_5424_full_record_bytes_and_str_agree():
    expected = ParsedLine(
        format="rfc5424",
        facility=4,
        severity=2,
        event_time=datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc),
        host="Router-A",
        message="Failed login",
        raw=LINE_5424,
    )
    assert parse_line(LINE_5424.encode()) == expected
    assert parse_line(LINE_5424) == expected


def test_5424_offset_is_kept():
    r = parse_line(p5424("2026-10-26T10:00:00+01:00 h a - - - hi"))
    assert r.event_time.utcoffset() == timedelta(hours=1)
    assert r.event_time == datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)


def test_5424_negative_offset_and_lowest_fraction():
    r = parse_line(p5424("2026-10-26T10:00:00.5-05:30 h a - - - hi"))
    assert r.event_time.utcoffset() == -timedelta(hours=5, minutes=30)
    assert r.event_time.microsecond == 500000


def test_5424_fraction_kept_to_the_microsecond():
    r = parse_line(p5424("2026-10-26T10:00:00.123456Z h a - - - hi"))
    assert r.event_time.microsecond == 123456


def test_5424_nil_timestamp_gives_no_event_time():
    r = parse_line(p5424("- router1 app - - - hi"))
    assert r.event_time is None
    assert r.host == "router1"
    assert r.message == "hi"


def test_5424_nil_host_is_kept_as_a_dash():
    assert parse_line(p5424("- - app - - - hi")).host == "-"


def test_5424_one_structured_data_element_skipped():
    r = parse_line(p5424('2026-10-26T10:00:00Z h app - - [a@1 k="v w"] hello there'))
    assert r.message == "hello there"


def test_5424_several_structured_data_elements_skipped():
    r = parse_line(p5424('- h app - - [a@1 k="v"][b@2 x="y" z="w"][c] hello'))
    assert r.message == "hello"


def test_5424_structured_data_with_escaped_quote_and_bracket():
    r = parse_line(p5424(r'- h app - - [a@1 k="q\"x\]y\\z"] after'))
    assert r.message == "after"


def test_5424_line_ending_right_after_structured_data_has_empty_message():
    assert parse_line(p5424('- h app - - [a@1 k="v"]')).message == ""
    assert parse_line(p5424("- h app - - -")).message == ""


def test_5424_byte_order_mark_removed_from_the_message():
    r = parse_line(p5424("- h app - - - ﻿hello").encode())
    assert r.message == "hello"


def test_5424_tab_in_the_message_is_kept():
    assert parse_line(p5424("- h app - - - a\tb")).message == "a\tb"


def test_5424_message_keeps_its_own_spaces():
    assert parse_line(p5424("- h app - - -  two  ")).message == " two  "


def test_pri_arithmetic():
    cases = {"<0>": (0, 0), "<34>": (4, 2), "<13>": (1, 5), "<191>": (23, 7), "<165>": (20, 5)}
    for pri, (facility, severity) in cases.items():
        r = parse_line(p5424(pri=pri))
        assert (r.facility, r.severity) == (facility, severity)


# --- RFC 3164 ---------------------------------------------------------------


def test_3164_record_has_no_event_time():
    line = "<13>Oct  6 10:00:00 router-a app: hi"
    r = parse_line(line.encode())
    assert r == ParsedLine("rfc3164", 1, 5, None, "router-a", "app: hi", line)


def test_3164_zero_padded_day_is_read_the_same():
    r = parse_line("<13>Oct 06 10:00:00 router-a app: hi")
    assert r.format == "rfc3164"
    assert r.event_time is None
    assert r.host == "router-a"
    assert r.message == "app: hi"


def test_3164_two_digit_day():
    r = parse_line("<34>Oct 11 22:14:15 router1 su: 'su root' failed")
    assert (r.severity, r.host, r.message) == (2, "router1", "su: 'su root' failed")


def test_3164_host_only_has_empty_message():
    assert parse_line("<13>Oct  6 10:00:00 router-a").message == ""


# --- host mapping -----------------------------------------------------------


def test_map_host_exact():
    m = map_host("Router-A", ["Router-A", "Router-B"])
    assert (m.status, m.device, m.candidates) == ("mapped", "Router-A", ("Router-A",))


def test_map_host_case_insensitive_unique():
    m = map_host("router-a", ["Router-A"])
    assert (m.status, m.device, m.candidates) == ("mapped", "Router-A", ("Router-A",))


def test_map_host_collision_is_ambiguous_with_sorted_candidates():
    m = map_host("ROUTER-A", ["router-a", "Router-A"])
    assert (m.status, m.device, m.candidates) == ("ambiguous", None, ("Router-A", "router-a"))


def test_map_host_exact_case_wins_in_a_collision():
    m = map_host("router-a", ["Router-A", "router-a"])
    assert (m.status, m.device, m.candidates) == ("mapped", "router-a", ("router-a",))


@pytest.mark.parametrize("host", ["-", "edge-9", "router-a.lab.local", "10.0.0.1", "Router", "Router-A-2"])
def test_map_host_unmapped_never_guesses(host):
    m = map_host(host, ["Router-A"])
    assert (m.status, m.device, m.candidates) == ("unmapped", None, ())


def test_map_host_no_devices_is_unmapped():
    assert map_host("Router-A", []).status == "unmapped"


def test_map_host_nil_host_is_unmapped_even_if_a_device_is_named_dash():
    assert map_host("-", ["-"]).status == "unmapped"


def test_map_host_accepts_any_iterable_once():
    assert map_host("a", iter(["A"])).status == "mapped"
    assert map_host("a", {"A"}).device == "A"


def test_a_readable_line_with_an_unknown_host_is_still_a_parsed_line():
    r = parse_line(p5424("- other app - - - x"))
    assert isinstance(r, ParsedLine)
    assert map_host(r.host, ["Router-A"]).status == "unmapped"
