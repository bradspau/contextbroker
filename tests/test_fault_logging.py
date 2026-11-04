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
        out = tt.assess_tmf921_as_of(tt.IID, "5m", as_of=None, mode="within",
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


# ---------- broker_engine/tsdb_joiner.py ----------
def _vm_vector(value="5"):
    from unittest.mock import MagicMock
    r = MagicMock()
    r.json.return_value = {"data": {"result": [{"value": [1, value]}]}}
    return r


def test_sample_time_query_failure_logs_class_and_reason(caplog):
    import requests as rq
    from unittest.mock import patch
    from broker_engine.tsdb_joiner import join_observed_metric
    with patch("broker_engine.tsdb_joiner.requests.get",
               side_effect=[_vm_vector(), rq.ConnectionError(CANARY)]):
        with caplog.at_level(logging.WARNING):
            env = join_observed_metric("r1", "e0", "latency_ms", "5m", now=NOW)
    assert env.observed_at is None and env.quality.stale is True  # unchanged: stale
    assert_clean(caplog, "sample time query failed", "ConnectionError", env.to_dict())


# ---------- intent_management/tmf921_client.py ----------
def test_intent_bounds_unreadable_logs_class_and_reason(caplog):
    from intent_management.tmf921_client import intent_bounds
    doc = {"expression": {"expressionValue": f"this is not turtle {CANARY}"}}
    with caplog.at_level(logging.WARNING):
        out = intent_bounds(doc)
    assert out == {}
    assert_clean(caplog, "intent goal unreadable", "BadSyntax", out)


def test_get_json_transport_error_logs_class_and_reason(caplog):
    import requests as rq
    from unittest.mock import patch
    from intent_management.tmf921_client import Tmf921Client
    with patch("intent_management.tmf921_client.requests.get",
               side_effect=rq.ConnectionError(CANARY)):
        with caplog.at_level(logging.WARNING):
            with pytest.raises(Tmf921Error) as ei:
                Tmf921Client("http://10.9.9.9:8000/x").get_intent("hsi-demo-1")
    assert str(ei.value) == "TMF921 request failed"
    assert_clean(caplog, "TMF921 request failed", "ConnectionError", ei.value)


def test_get_json_non_2xx_does_not_log(caplog):
    from unittest.mock import MagicMock, patch
    from intent_management.tmf921_client import Tmf921Client
    r = MagicMock()
    r.status_code = 500
    with patch("intent_management.tmf921_client.requests.get", return_value=r):
        with caplog.at_level(logging.WARNING):
            with pytest.raises(Tmf921Error):
                Tmf921Client("http://10.9.9.9:8000/x").get_intent("hsi-demo-1")
    assert caplog.records == []


def test_post_observation_transport_error_logs_class_and_reason(caplog):
    import requests as rq
    from unittest.mock import patch
    from intent_management.tmf921_client import Tmf921Client
    with patch("intent_management.tmf921_client.requests.post",
               side_effect=rq.Timeout(CANARY)):
        with caplog.at_level(logging.WARNING):
            with pytest.raises(Tmf921Error) as ei:
                Tmf921Client("http://10.9.9.9:8000/x").post_observation("hsi-demo-1", URI, 5.0, "ms")
    assert str(ei.value) == "TMF921 request failed"
    assert_clean(caplog, "TMF921 request failed", "Timeout", ei.value)


# ---------- outcome_assurance/tmf921_report.py ----------
def test_turtle_unreadable_logs_class_and_reason(caplog, monkeypatch):
    import rdflib
    from outcome_assurance import tmf921_report as rpt

    def boom(self, *a, **kw):
        raise ValueError(CANARY)
    monkeypatch.setattr(rdflib.Graph, "parse", boom)
    with caplog.at_level(logging.WARNING):
        out = rpt._turtle_outcome("@prefix x: <http://x/> .")
    assert out is None
    assert_clean(caplog, "report Turtle unreadable", "ValueError", out)


def test_interpret_report_failure_logs_class_and_reason(caplog, monkeypatch):
    from outcome_assurance import tmf921_report as rpt

    def boom(*a, **kw):
        raise KeyError(CANARY)
    monkeypatch.setattr(rpt, "_interpret", boom)
    with caplog.at_level(logging.WARNING):
        verdict = rpt.interpret_report({"id": "r"}, now=NOW)
    assert verdict.outcome.value == "inconclusive" and verdict.reasons == ["unparseable report"]
    assert_clean(caplog, "unparseable report", "KeyError", verdict)


# ---------- broker_engine/main.py ----------
def test_rest_upstream_error_logs_class_and_reason(caplog):
    import requests as rq
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from broker_engine import main
    from tests.auth_helpers import auth_headers
    c = TestClient(main.app, headers=auth_headers("context:read"), raise_server_exceptions=False)
    params = {"device": "r1", "interface": "e0", "metric_type": "latency_ms",
              "time_window": "5m"}
    with patch("broker_engine.tsdb_joiner.requests.get",
               side_effect=rq.ConnectionError(CANARY)):
        with caplog.at_level(logging.WARNING):
            r = c.get("/context/metric", params=params)
    assert r.status_code == 502
    assert r.json() == {"detail": "upstream data store unavailable"}
    assert_clean(caplog, "upstream data store unavailable", "ConnectionError", r.text)
