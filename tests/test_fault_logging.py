"""Fail-closed handlers log the exception class and a fixed reason, never server text.

Each canary test raises an exception whose text carries a URL and a marker, then
asserts the fixed reason and the class name are logged, the marker and URL are
not, no traceback text is attached, and the returned value is unchanged."""
import logging

import pytest

from broker_engine.faults import log_fault
from broker_engine.validation import ClientInputError
from intent_management import tmf921_source as src
from intent_management.tmf921_client import Tmf921Error
from test_tmf921_source import (  # noqa: F401  (fixtures and helpers)
    BIND2, IID, NOW, URI, URI2, FakeClient, SecondPostFails, clean_store, intent,
    make_join, no_live_graph, rep, run,
)

CANARY = "CANARY-7f3a http://10.9.9.9:3030/secret"


def assert_clean(caplog, reason, cls, *returned):
    assert f"{reason}: {cls}" in caplog.text
    assert "CANARY" not in caplog.text and "10.9.9.9" not in caplog.text
    for r in caplog.records:
        assert r.exc_info is None and r.exc_text is None
        assert r.levelno == logging.WARNING
    for value in returned:
        assert "CANARY" not in str(value) and "10.9.9.9" not in str(value)


def test_log_fault_writes_class_and_reason_only(caplog):
    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError("str() must never be called")

    log = logging.getLogger("tests.faults")
    with caplog.at_level(logging.WARNING):
        log_fault(log, ConnectionError(CANARY), "upstream data store unavailable")
        log_fault(log, Hostile(), "fixed reason")
    assert [r.getMessage() for r in caplog.records] == [
        "upstream data store unavailable: ConnectionError", "fixed reason: Hostile"]
    assert all(r.levelno == logging.WARNING and r.exc_info is None and r.exc_text is None
               for r in caplog.records)
    assert "CANARY" not in caplog.text


# ---------- intent_management/tmf921_source.py ----------
def test_get_intent_before_push_logs_class_and_reason(caplog):
    with caplog.at_level(logging.WARNING):
        out = run(FakeClient(get_exc=ConnectionError(CANARY)))
    assert out["reasons"] == ["intent server unavailable"]
    assert_clean(caplog, "intent server unavailable", "ConnectionError", out)


def test_push_and_poll_block_logs_class_and_reason(caplog):
    with caplog.at_level(logging.WARNING):
        out = run(FakeClient(list_exc=RuntimeError(CANARY)))
    assert out["reasons"] == ["intent server unavailable"]
    assert_clean(caplog, "intent server unavailable", "RuntimeError", out)


def test_first_push_failure_logs_and_keeps_its_reason(caplog):
    with caplog.at_level(logging.WARNING):
        out = run(FakeClient(push_exc=Tmf921Error(CANARY)))
    assert out["reasons"] == ["intent server unavailable"]
    assert_clean(caplog, "intent server unavailable", "Tmf921Error", out)


def test_goal_recheck_after_success_logs_class_and_reason(caplog):
    class RecheckFails(FakeClient):
        def get_intent(self, i):
            self.gets += 1
            if self.gets > 1:
                raise TimeoutError(CANARY)
            return self.intent_doc

    c = RecheckFails(post=[[rep("n", "2026-10-27T12:00:01Z")]])
    with caplog.at_level(logging.WARNING):
        out = run(c)
    assert out["verdict"] == "inconclusive" and out["reasons"] == ["intent server unavailable"]
    assert_clean(caplog, "intent server unavailable", "TimeoutError", out)


def test_temporal_get_intent_logs_class_and_reason(caplog):
    import test_temporal_tmf921 as tt
    with caplog.at_level(logging.WARNING):
        out = tt.assess_tmf921_as_of(tt.IID, "5m", as_of="10m", mode=None,
                                     client=tt.FakeClient(get_exc=ConnectionError(CANARY)),
                                     join=tt.make_join({}), now=tt.NOW, metric_map=tt.MMAP,
                                     bindings=tt.B_LAT)
    assert out["reasons"] == ["intent server unavailable"]
    assert_clean(caplog, "intent server unavailable", "ConnectionError", out)


@pytest.mark.parametrize("exc,cls", [(Tmf921Error(CANARY), "Tmf921Error"),
                                     (ClientInputError(CANARY), "ClientInputError")])
def test_partial_push_logs_class_and_reason(caplog, exc, cls):
    c = SecondPostFails(exc, intent_doc=intent((URI, 100), (URI2, 10)))
    with caplog.at_level(logging.WARNING):
        out = run(c, bindings=BIND2)
    assert out["reasons"] == ["partial push: pushed LatencyMetric; failed JitterMetric"]
    assert_clean(caplog, "partial push", cls, out)
