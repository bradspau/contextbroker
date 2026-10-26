from datetime import datetime, timedelta, timezone

from data_contracts.envelope import ContextEnvelope, Quality, StateCategory
from ingestion.syslog_parser import SyslogEvent
from state_reconciliation.event_reconciler import reconcile

NOW = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)


def ev(device="d1", t=NOW, msg="interface up"):
    return SyslogEvent(raw="RAW LINE", device_id=device, severity=5, event_time=t, message=msg)


def cfg(value="interface up"):
    return ContextEnvelope("d1", StateCategory.CONFIGURED, value, "netconf", NOW, Quality())


def test_unresolved_device():
    r = reconcile(ev(device=None), None, now=NOW)
    assert r.envelope is None
    assert r.conflicts == ("unresolved device",)


def test_current_none_gives_observed():
    r = reconcile(ev(), None, now=NOW)
    assert r.envelope.state is StateCategory.OBSERVED
    assert r.envelope.source == "syslog"
    assert r.envelope.entity_id == "d1"
    assert r.envelope.value == "interface up"
    assert r.conflicts == ()
    assert not r.envelope.quality.stale and not r.envelope.quality.missing


def test_missing_event_time():
    r = reconcile(ev(t=None), None, now=NOW)
    assert r.envelope.quality.missing is True
    assert r.envelope.value == "interface up"
    assert r.envelope.observed_at is None


def test_stale_event():
    r = reconcile(ev(t=NOW - timedelta(hours=1)), None, now=NOW)
    assert r.envelope.quality.stale is True


def test_contradiction_reported_current_untouched():
    cur = cfg("interface down")
    r = reconcile(ev(), cur, now=NOW)
    assert r.conflicts == ("event contradicts configured state for d1",)
    assert r.envelope.quality.conflicts == list(r.conflicts)
    assert r.envelope.state is StateCategory.OBSERVED
    assert cur.value == "interface down" and cur.state is StateCategory.CONFIGURED
    assert r.envelope is not cur and cur.quality.conflicts == []


def test_agreeing_config_no_conflict():
    r = reconcile(ev(), cfg("  INTERFACE UP "), now=NOW)
    assert r.conflicts == ()
    assert r.envelope.quality.conflicts == []


def test_provenance():
    r = reconcile(ev(), None, now=NOW)
    p = r.envelope.provenance
    assert p["raw"] == "RAW LINE" and p["severity"] == 5
    assert p["reconciler"] == "system1-event-reconciler-stub"
    assert p["reconciler_version"] == "0.1"


def test_never_configured():
    for cur in (None, cfg("x")):
        assert reconcile(ev(), cur, now=NOW).envelope.state is not StateCategory.CONFIGURED


def test_mismatched_current_ignored():
    cur = ContextEnvelope("other", StateCategory.CONFIGURED, "different", "netconf", NOW, Quality())
    r = reconcile(ev(), cur, now=NOW)
    assert r.conflicts == ()
    assert cur.value == "different" and cur.quality.conflicts == []


def test_naive_now_raises():
    import pytest
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        reconcile(ev(), None, now=datetime(2026, 10, 26, 10, 0))
