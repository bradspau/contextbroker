"""OPS-01: GET /health reports Fuseki, VictoriaMetrics, TMF921 and the state store with fixed strings, no credentials."""
import logging
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient

from broker_engine import health, main
from broker_state.process import get_store, reset_store
from broker_state.store import NewEvent, StateStore, StateUnavailable

ZERO_EVENTS = {"duplicates": 0, "unmapped": 0,
               "quarantined": {"no priority": 0, "not text": 0, "too long": 0, "unparsable header": 0}}
OK_BODY = {"status": "ok", "fuseki": "answering", "victoriametrics": "answering", "tmf921": "answering",
           "state": "answering",
           "evictions": {"outcomes": 0, "marks": 0, "situations": 0, "events": 0},
           "events": ZERO_EVENTS}
CANARY = "CANARY http://10.9.9.9:3030"
client = TestClient(main.app)


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
    health._reset_cache()
    yield
    health._reset_cache()


def _resp(status=200):
    r = MagicMock()
    r.status_code = status
    return r


def _which(url):
    if "/$/ping" in url:
        return "fuseki"
    if ":8428" in url:
        return "victoriametrics"
    return "tmf921"


def _dispatch(overrides=None, calls=None):
    """requests.get replacement: 200 everywhere except the named upstreams."""
    overrides = overrides or {}

    def get(url, **kwargs):
        if calls is not None:
            calls.append((url, kwargs))
        outcome = overrides.get(_which(url), 200)
        if isinstance(outcome, BaseException):
            raise outcome
        return _resp(outcome)

    return get


def test_all_answering_is_200_with_exactly_seven_keys():
    with patch("broker_engine.health.requests.get", side_effect=_dispatch()):
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == OK_BODY
    assert len(r.json()) == 7


@pytest.mark.parametrize("name", ["fuseki", "victoriametrics", "tmf921"])
@pytest.mark.parametrize("fault", [
    requests.ConnectionError(CANARY),
    requests.Timeout(CANARY),
    500,
], ids=["connection", "timeout", "http500"])
def test_one_upstream_down_is_503_degraded_without_canary(name, fault):
    with patch("broker_engine.health.requests.get", side_effect=_dispatch({name: fault})):
        r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    expected = dict(OK_BODY, status="degraded")
    expected[name] = "not answering"
    assert body == expected
    assert "CANARY" not in r.text and "10.9.9.9" not in r.text and "3030" not in r.text


def test_probe_urls_timeout_and_redirects():
    calls = []
    with patch("broker_engine.health.requests.get", side_effect=_dispatch(calls=calls)):
        client.get("/health")
    assert len(calls) == 3
    by_name = {_which(url): (url, kw) for url, kw in calls}
    assert set(by_name) == {"fuseki", "victoriametrics", "tmf921"}
    assert by_name["fuseki"][0].endswith("/$/ping")
    assert by_name["victoriametrics"][0].endswith("/health")
    assert by_name["tmf921"][0].endswith("/health")
    for _, kw in by_name.values():
        assert kw["timeout"] == 2
        assert kw["allow_redirects"] is False
    # the probe URLs are origins: no query path of the endpoint constants is carried over
    assert "/query" not in by_name["fuseki"][0]
    assert "/api/v1" not in by_name["victoriametrics"][0]
    assert "/tmf-api" not in by_name["tmf921"][0]


def test_fuseki_probe_carries_the_credential_only_when_set(monkeypatch):
    calls = []
    with patch("broker_engine.health.requests.get", side_effect=_dispatch(calls=calls)):
        client.get("/health")
    fuseki = [kw for url, kw in calls if _which(url) == "fuseki"][0]
    assert fuseki.get("auth") is None

    health._reset_cache()
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "abc123")
    calls = []
    with patch("broker_engine.health.requests.get", side_effect=_dispatch(calls=calls)):
        client.get("/health")
    fuseki = [kw for url, kw in calls if _which(url) == "fuseki"][0]
    others = [kw for url, kw in calls if _which(url) != "fuseki"]
    assert fuseki["auth"] == ("admin", "abc123")
    assert all(kw.get("auth") is None for kw in others)


def test_probes_run_in_parallel():
    def slow(url, **kwargs):
        time.sleep(0.5)
        return _resp(200)

    with patch("broker_engine.health.requests.get", side_effect=slow):
        start = time.monotonic()
        r = client.get("/health")
        elapsed = time.monotonic() - start
    assert r.status_code == 200
    assert elapsed < 1.5


def test_repeated_requests_within_the_window_share_one_set_of_probes():
    calls = []
    with patch("broker_engine.health.requests.get", side_effect=_dispatch(calls=calls)):
        for _ in range(20):
            client.get("/health")
        assert len(calls) == 3
        # after the window, three more
        with patch("broker_engine.health.time.monotonic", return_value=time.monotonic() + 6):
            client.get("/health")
    assert len(calls) == 6


def test_undeclared_query_parameter_is_422():
    with patch("broker_engine.health.requests.get", side_effect=_dispatch()) as get:
        r = client.get("/health", params={"x": "1"})
    assert r.status_code == 422
    get.assert_not_called()


def test_health_answers_with_no_jwt_key_while_context_is_401(monkeypatch):
    for name in ("BROKER_JWT_PUBLIC_KEY", "BROKER_JWT_ISSUER", "BROKER_JWT_PUBLIC_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    with patch("broker_engine.health.requests.get", side_effect=_dispatch()):
        r = client.get("/health")
    assert r.status_code == 200
    with patch("requests.get") as get:
        denied = client.get("/context/metric", params={
            "device": "r1", "interface": "e0", "metric_type": "latency_ms", "time_window": "5m"})
    assert denied.status_code == 401
    get.assert_not_called()


def test_failed_probe_logs_class_or_status_never_the_canary(caplog):
    overrides = {"fuseki": requests.ConnectionError(CANARY), "tmf921": 500}
    with caplog.at_level(logging.WARNING), \
            patch("broker_engine.health.requests.get", side_effect=_dispatch(overrides)):
        client.get("/health")
    text = caplog.text
    assert "health probe failed: fuseki: ConnectionError" in text
    assert "health probe failed: tmf921: HTTP 500" in text
    assert "CANARY" not in text and "10.9.9.9" not in text
    assert "victoriametrics" not in text


# ---- event evictions and event counters (D-22) ----

HOST_CANARY = "canary-host-77.example"
DEVICE_CANARY = "CanaryDevice-77"
LINE_CANARY = "canary line text 77"
NOW = datetime.now(timezone.utc)


def _event(n, mapped=True):
    return NewEvent(
        event_id="evt-" + f"{n:032x}", ingest_time=NOW, event_time=None, severity=4, facility=3,
        format="rfc5424", source_host=HOST_CANARY,
        mapping="mapped" if mapped else "unmapped",
        device=DEVICE_CANARY if mapped else None,
        candidates=(DEVICE_CANARY,) if mapped else (), kind=None, raw=f"<28>1 {LINE_CANARY} {n}")


def _body():
    with patch("broker_engine.health.requests.get", side_effect=_dispatch()):
        return client.get("/health")


def _all_ints(value):
    if isinstance(value, dict):
        return all(_all_ints(v) for v in value.values())
    return type(value) is int


def test_events_row_counts_duplicates_unmapped_and_quarantined_lines_per_reason():
    store = get_store()
    store.add_events([_event(1, mapped=False)], quarantined={"too long": 2})
    store.add_events([_event(1, mapped=False)])
    health._reset_cache()
    r = _body()
    assert r.status_code == 200
    assert r.json()["events"] == {
        "duplicates": 1, "unmapped": 1,
        "quarantined": {"no priority": 0, "not text": 0, "too long": 2, "unparsable header": 0}}
    assert _all_ints(r.json()["events"]) and _all_ints(r.json()["evictions"])
    assert HOST_CANARY not in r.text and DEVICE_CANARY not in r.text and LINE_CANARY not in r.text
    assert "sqlite" not in r.text and "tmp" not in r.text


def test_events_counts_survive_a_reopen_of_the_state_file():
    store = get_store()
    store.add_events([_event(1)], quarantined={"no priority": 3})
    store.add_events([_event(1)])
    reset_store()
    health._reset_cache()
    r = _body()
    assert r.json()["events"]["duplicates"] == 1
    assert r.json()["events"]["quarantined"]["no priority"] == 3


def test_events_evictions_show_the_oldest_first_cap(monkeypatch):
    monkeypatch.setenv("BROKER_EVENT_CAP", "1")
    reset_store()
    get_store().add_events([_event(1), _event(2)])
    health._reset_cache()
    assert _body().json()["evictions"] == {"outcomes": 0, "marks": 0, "situations": 0, "events": 1}


@pytest.mark.parametrize("target", ["ping", "evictions", "event_counters"])
def test_events_are_null_when_the_state_store_does_not_answer(target):
    with patch.object(StateStore, target, side_effect=StateUnavailable()):
        r = _body()
    assert r.status_code == 503
    body = r.json()
    assert body["state"] == "not answering" and body["status"] == "degraded"
    assert body["evictions"] is None and body["events"] is None
    assert len(body) == 7


def test_events_counters_are_read_once_per_window():
    real_counters, real_evictions = StateStore.event_counters, StateStore.evictions
    reads = []

    def counters(self):
        reads.append("counters")
        return real_counters(self)

    def evictions(self):
        reads.append("evictions")
        return real_evictions(self)

    with patch.object(StateStore, "event_counters", autospec=True, side_effect=counters), \
            patch.object(StateStore, "evictions", autospec=True, side_effect=evictions):
        for _ in range(20):
            assert _body().status_code == 200
    assert sorted(reads) == ["counters", "evictions"]
