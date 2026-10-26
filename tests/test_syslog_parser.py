from datetime import datetime, timedelta, timezone

from ingestion.syslog_parser import parse_syslog

DEVS = {"Router1": "dev-r1"}


def test_5424_z_timestamp():
    line = "<34>1 2026-10-26T10:00:00Z router1 sshd 123 ID47 - Failed login"
    e = parse_syslog(line, DEVS)
    assert e.event_time == datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
    assert e.device_id == "dev-r1"
    assert e.severity == 2
    assert e.message == "Failed login"
    assert e.raw == line


def test_5424_offset_timestamp():
    e = parse_syslog("<13>1 2026-10-26T10:00:00+01:00 router1 app - - - hi", DEVS)
    assert e.event_time.utcoffset() == timedelta(hours=1)
    assert e.severity == 5


def test_5424_nilvalue_timestamp():
    e = parse_syslog("<13>1 - router1 app - - - hi", DEVS)
    assert e.event_time is None
    assert e.device_id == "dev-r1"


def test_5424_naive_timestamp_is_none():
    e = parse_syslog("<13>1 2026-10-26T10:00:00 router1 app - - - hi", DEVS)
    assert e.event_time is None


def test_5424_structured_data_skipped():
    e = parse_syslog('<13>1 2026-10-26T10:00:00Z router1 app - - [a@1 k="v w"] hello there', DEVS)
    assert e.message == "hello there"


def test_3164_event_time_none():
    e = parse_syslog("<34>Oct 11 22:14:15 router1 su: 'su root' failed", DEVS)
    assert e.severity == 2
    assert e.event_time is None
    assert e.device_id == "dev-r1"
    assert e.message == "su: 'su root' failed"


def test_unknown_hostname():
    assert parse_syslog("<13>1 - other app - - - x", DEVS).device_id is None


def test_case_insensitive_hostname():
    assert parse_syslog("<13>1 - ROUTER1 app - - - x", DEVS).device_id == "dev-r1"


def test_pri_math():
    assert parse_syslog("<34>1 - router1 a - - - x", DEVS).severity == 2
    assert parse_syslog("<191>1 - router1 a - - - x", DEVS).severity == 7


def test_malformed_does_not_raise():
    for bad in ["", "   ", "garbage line", "<abc>1 x", None, 42, b"<34>1"]:
        e = parse_syslog(bad, DEVS)
        assert e.raw is bad or e.raw == bad
        assert e.device_id is None and e.severity is None and e.event_time is None
    assert parse_syslog("  garbage  ", DEVS).message == "garbage"
    assert parse_syslog("  garbage  ", DEVS).raw == "  garbage  "


def test_raw_always_retained():
    line = "<34>1 2026-10-26T10:00:00Z router1 a - - - msg  "
    assert parse_syslog(line, DEVS).raw == line
