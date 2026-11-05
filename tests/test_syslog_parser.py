"""Strict syslog parser: a line is a ParsedLine or a Quarantined reason (D-20, D-21)."""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from data_contracts.syslog_events import MAX_LINE_BYTES, QUARANTINE_REASONS, ParsedLine, Quarantined
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
    r = parse_line(p5424("- h app - - - \ufeffhello").encode())
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


# --- quarantine (D-20, D-21) --------------------------------------------------

GOOD = "<13>1 - h a - - - x"


def reason(line):
    r = parse_line(line)
    assert isinstance(r, Quarantined), r
    return r.reason


def test_quarantine_too_long_is_checked_on_the_bytes_before_decoding():
    assert reason(b"\xff" * 5000) == "too long"  # not "not text": length comes first


def test_quarantine_never_decodes_an_over_long_line():
    class Boom(bytes):
        def decode(self, *a, **k):
            raise AssertionError("decoded an over-long line")

    assert reason(Boom(b"x" * 5000)) == "too long"


def test_quarantine_length_boundary_4096_parses_4097_does_not():
    pad = MAX_LINE_BYTES - len(GOOD)
    at_cap = GOOD + "y" * pad
    assert len(at_cap.encode()) == MAX_LINE_BYTES
    assert isinstance(parse_line(at_cap.encode()), ParsedLine)
    assert isinstance(parse_line(at_cap), ParsedLine)
    assert reason((at_cap + "y").encode()) == "too long"
    assert reason(at_cap + "y") == "too long"


def test_quarantine_length_of_a_str_counts_utf_8_bytes():
    just_over = GOOD + "é" * ((MAX_LINE_BYTES - len(GOOD)) // 2 + 1)
    assert len(just_over) < MAX_LINE_BYTES < len(just_over.encode())
    assert reason(just_over) == "too long"


def test_quarantine_too_long_wins_over_every_later_check():
    assert reason(b"\x00" * 5000) == "too long"
    assert reason("hello" * 2000) == "too long"


@pytest.mark.parametrize("bad", [None, 7, 1.5, ["<13>"], {"a": 1}, bytearray(GOOD.encode())])
def test_quarantine_not_text_for_a_wrong_type(bad):
    assert reason(bad) == "not text"


@pytest.mark.parametrize(
    "bad",
    [
        b"\xff\xfe<13>1 - h a - - - x",
        b"<13>1 - h a - - - \xc3",  # truncated multi-byte sequence
        b"<13>1 - h a - - - \xed\xa0\x80",  # an encoded surrogate
        "<13>1 - h a - - - \ud800",  # a lone surrogate in a str
        b"\x00",
        "<13>1 - h a - - - a\x00b",
        "<13>1 - h a - - - a\x1bb",
        "<13>1 - h a - - - a\rb",
        "<13>1 - h a - - - a\nb",
        "<13>1 - h a - - - a\x7fb",
        "<13>1 - h a - - - a\x85b",
        "<13>1 - h a - - - x\n",
    ],
)
def test_quarantine_not_text(bad):
    assert reason(bad) == "not text"


def test_quarantine_not_text_wins_over_no_priority():
    assert reason(b"\xff\xfehello") == "not text"
    assert reason("hello\x00") == "not text"


@pytest.mark.parametrize(
    "bad",
    [
        "hello",
        "",
        b"",
        " <13>1 - h a - - - x",
        "<192>1 - h a - - - x",
        "<999>1 - h a - - - x",
        "<01>1 - h a - - - x",
        "<00>1 - h a - - - x",
        "<>1 - h a - - - x",
        "<1234>1 - h a - - - x",
        "<-1>1 - h a - - - x",
        "<1 3>1 - h a - - - x",
        "<١٣>1 - h a - - - x",  # Arabic-Indic digits
        "13>1 - h a - - - x",
        "<13 1 - h a - - - x",
    ],
)
def test_quarantine_no_priority(bad):
    assert reason(bad) == "no priority"


@pytest.mark.parametrize(
    "bad",
    [
        "<13>",
        "<13>Foo 99 99:99:99 host x",
        "<13>oct  6 10:00:00 host x",  # month is case-sensitive
        "<13>Oct 32 10:00:00 host x",
        "<13>Oct  0 10:00:00 host x",
        "<13>Feb 30 10:00:00 host x",
        "<13>Oct  6 24:00:00 host x",
        "<13>Oct  6 10:60:00 host x",
        "<13>Oct  6 10:00:60 host x",
        "<13>Oct  6 10:00:00",  # no host
        "<13>Oct  6 10:00:00 ",
        "<13>Oct 6 10:00:00 host x",  # day not padded
        "<13>Oct  6 10:00:00 " + "h" * 256 + " x",
        "<13>Oct  6 10:00:00 hé x",
        "<13>2 - h a - - - x",  # version 2
        "<13>1",
        "<13>1 ",
        "<13>1 - h a - -",  # fewer than six header fields
        "<13>1 - h a - - x",  # structured data is mandatory ("x" is neither "-" nor "[")
        "<13>1 2026-10-26T10:00:00 h a - - - x",  # zoneless
        "<13>1 2026-10-26t10:00:00Z h a - - - x",
        "<13>1 2026-10-26T10:00:00z h a - - - x",
        "<13>1 2026-10-26T10:00:00.1234567Z h a - - - x",
        "<13>1 2026-10-26 h a - - - x",
        "<13>1 20261026T100000Z h a - - - x",
        "<13>1 2026-10-26T10:00:00+0100 h a - - - x",
        "<13>1 2026-13-26T10:00:00Z h a - - - x",
        "<13>1 2026-10-26T24:00:00Z h a - - - x",
        "<13>1 2026-02-30T10:00:00Z h a - - - x",
        "<13>1 2026-10-26T10:00:60Z h a - - - x",
        "<13>1 2026-10-26T10:00:00+24:00 h a - - - x",
        "<13>1 - " + "h" * 256 + " a - - - x",
        "<13>1 - hé a - - - x",
        "<13>1 - h " + "a" * 49 + " - - - x",
        "<13>1 - h a " + "p" * 129 + " - - x",
        "<13>1 - h a - " + "m" * 33 + " - x",
        '<13>1 - h a - - [a@1 k="v" x',  # unterminated structured data
        '<13>1 - h a - - [a@1 k="v"]x',  # no space after structured data
        '<13>1 - h a - - [a@1 k=v] x',  # unquoted parameter value
        "<13>1 - h a - - [] x",
        "<13>1 - h a - -  - x",  # two spaces between fields
        "<34>2026-10-26T10:00:00.123456+01:00 host tag: msg",  # rsyslog file form: known limit, pinned
    ],
)
def test_quarantine_unparsable_header(bad):
    assert reason(bad) == "unparsable header"


def test_quarantine_pinned_order_end_to_end():
    assert [reason(x) for x in (b"\xff" * 5000, b"\x00", b"hello", b"<13>Foo 99 99:99:99 h x")] == [
        "too long",
        "not text",
        "no priority",
        "unparsable header",
    ]


def test_every_reason_is_one_of_the_four_fixed_strings():
    for line in (b"\xff" * 5000, b"\x00", b"hello", b"<13>"):
        assert reason(line) in QUARANTINE_REASONS
    assert QUARANTINE_REASONS == ("no priority", "not text", "too long", "unparsable header")


def test_quarantined_holds_only_the_reason():
    q = parse_line(b"<13>CANARY-LINE-42 99 99:99:99 h x")
    assert [f.name for f in dataclasses.fields(q)] == ["reason"]
    assert "CANARY-LINE-42" not in repr(q) and "CANARY-LINE-42" not in str(q)
    assert not hasattr(q, "__dict__")  # slotted: no way to attach the line afterwards


@pytest.mark.parametrize("bad", ["CANARY-LINE-42", "CANARY-LINE-42" * 400, b"\xffCANARY-LINE-42"])
def test_quarantine_never_echoes_the_line(bad):
    q = parse_line(bad)
    assert isinstance(q, Quarantined)
    assert "CANARY-LINE-42" not in repr(q) and "CANARY-LINE-42" not in str(q)
    assert "CANARY" not in " ".join(str(getattr(q, f.name)) for f in dataclasses.fields(q))


def test_quarantine_does_not_log(caplog):
    with caplog.at_level("DEBUG"):
        parse_line(b"<13>CANARY-LINE-42")
        parse_line(b"\xff" * 5000)
    assert caplog.records == []


def test_quarantined_refuses_an_unknown_reason_without_echoing_it():
    with pytest.raises(ValueError) as e:
        Quarantined("CANARY-LINE-42")
    assert "CANARY" not in str(e.value)


def test_3164_feb_29_exists_but_feb_30_does_not():
    assert isinstance(parse_line("<13>Feb 29 10:00:00 h x"), ParsedLine)
    assert reason("<13>Feb 30 10:00:00 h x") == "unparsable header"


def test_parse_line_never_raises_on_odd_input():
    for odd in (None, 0, b"", "", object(), b"\x00" * 10, "\U0001f600" * 5, "<13>1 " + "[" * 3000):
        assert isinstance(parse_line(odd), (ParsedLine, Quarantined))


def test_parse_line_is_fast_on_hostile_structured_data_at_the_cap():
    import time

    pre = "<13>1 - h a - - "
    hostile = [
        pre + '[a@1 ' + 'k="\\' * 600,
        pre + "[" * 4000,
        pre + "[a]" * 1300 + "x",
        pre + '[a@1 k="' + "\\\\" * 1900 + '"',
    ]
    for line in hostile:
        started = time.perf_counter()
        assert isinstance(parse_line(line[:MAX_LINE_BYTES]), Quarantined)
        assert time.perf_counter() - started < 0.5
