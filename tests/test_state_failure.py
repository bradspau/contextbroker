"""D-04 and D-12 through the API: no start without a usable state file, /health reports the state store.

Start-up refusal is tested by entering `with TestClient(main.app)`, which runs the lifespan. Other test
files build a TestClient without a with block; they get the lazily opened per-test store instead.
"""
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from broker_engine import health, main
from broker_state.process import get_store, load_settings, reset_store
from broker_state.store import STATE_UNAVAILABLE, StateStore, StateUnavailable
from data_contracts.envelope import build_observed
from data_contracts.intent_bindings import IntentBinding
from test_context_request import (  # noqa: F401  (fixtures and helpers)
    ASSURANCE, SCOPES, TOPOLOGY, assure, client, live_goal, olt, rig, sample, tmf921_intent, token)
from test_situation_state import fetch, recorded
from tests.fluent_helpers import IF0, LAT, T0, bind

ROOT = Path(__file__).resolve().parent.parent
CANARY = "canary99x"


@pytest.fixture(autouse=True)
def fresh_cache():
    health._reset_cache()
    yield
    health._reset_cache()


def _answering(url, **kwargs):
    r = MagicMock()
    r.status_code = 200
    return r


# ---- start-up refusal ----

def test_a_missing_state_directory_stops_the_start_and_the_log_names_no_path(tmp_path, monkeypatch, caplog):
    missing = tmp_path / "no-such-dir" / "state.sqlite3"
    monkeypatch.setenv("BROKER_STATE_PATH", str(missing))
    with caplog.at_level(logging.WARNING), pytest.raises(StateUnavailable):
        with TestClient(main.app):
            pytest.fail("the application must not serve without its state store")
    assert "state store unavailable: FileNotFoundError" in caplog.text
    assert "no-such-dir" not in caplog.text and str(tmp_path) not in caplog.text
    assert not missing.exists()


@pytest.mark.parametrize("name", ["BROKER_OUTCOME_CAP", "BROKER_SITUATION_RETENTION",
                                  "BROKER_MARK_CAP", "BROKER_SITUATION_CAP",
                                  "BROKER_EVENT_CAP", "BROKER_EVENT_RETENTION",
                                  "BROKER_EVENT_WINDOW_DEFAULT", "BROKER_EVENT_WINDOW_MAX"])
def test_a_malformed_state_variable_stops_the_start_and_names_only_the_variable(monkeypatch, name):
    monkeypatch.setenv(name, CANARY)
    with pytest.raises(ValueError) as caught:
        with TestClient(main.app):
            pytest.fail("the application must not serve with a malformed setting")
    assert name in str(caught.value)
    assert CANARY not in str(caught.value)


def test_a_memory_state_path_is_refused_at_start_up(monkeypatch):
    monkeypatch.setenv("BROKER_STATE_PATH", ":memory:")
    with pytest.raises(ValueError, match="BROKER_STATE_PATH"):
        with TestClient(main.app):
            pytest.fail("no memory fallback")


def test_a_good_start_answers_and_the_shutdown_closes_the_store_cleanly(tmp_path):
    path = tmp_path / "broker-state.sqlite3"
    with patch("broker_engine.health.requests.get", side_effect=_answering):
        with TestClient(main.app) as c:
            assert c.get("/health").json()["state"] == "answering"
            get_store().record_outcome("intent-1", "success")
    # The lifespan closed and forgot the process store; a fresh store on the same file reads the row.
    reopened = StateStore(str(path))
    try:
        assert reopened.last_outcome("intent-1") == "success"
    finally:
        reopened.close()


def test_the_store_is_opened_once_per_start(monkeypatch):
    opened = []
    real = StateStore

    def counting(*a, **kw):
        opened.append(1)
        return real(*a, **kw)

    with patch("broker_state.process.StateStore", side_effect=counting):
        with TestClient(main.app) as c:
            with patch("broker_engine.health.requests.get", side_effect=_answering):
                c.get("/health")
                health._reset_cache()
                c.get("/health")
    assert len(opened) == 1


def test_importing_the_api_opens_no_state_file(tmp_path):
    path = tmp_path / "import-only.sqlite3"
    env = {"PATH": "/usr/bin:/bin", "BROKER_STATE_PATH": str(path)}
    done = subprocess.run([sys.executable, "-c", "import broker_engine.main"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert not path.exists()
    assert list(tmp_path.glob("import-only*")) == []


# ---- /health state row ----

def test_a_store_that_does_not_answer_makes_the_health_degraded_and_503(caplog):
    with patch.object(StateStore, "ping", side_effect=StateUnavailable()), \
            patch("broker_engine.health.requests.get", side_effect=_answering), \
            caplog.at_level(logging.WARNING):
        r = TestClient(main.app).get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["state"] == "not answering"
    assert [body[k] for k in ("fuseki", "victoriametrics", "tmf921")] == ["answering"] * 3
    assert "health probe failed: state: StateUnavailable" in caplog.text
    assert load_settings().path not in r.text


def test_twenty_health_requests_in_a_window_make_three_probes_and_one_ping():
    real_ping = StateStore.ping
    pings = []

    def spy(self):
        pings.append(1)
        return real_ping(self)

    http = []

    def get(url, **kwargs):
        http.append(url)
        return _answering(url)

    client = TestClient(main.app)
    with patch.object(StateStore, "ping", autospec=True, side_effect=spy), \
            patch("broker_engine.health.requests.get", side_effect=get):
        for _ in range(20):
            assert client.get("/health").status_code == 200
    assert len(http) == 3
    assert len(pings) == 1


# ---- /health eviction counters (D-12) ----

ZERO_EVICTIONS = {"outcomes": 0, "marks": 0, "situations": 0, "events": 0}


def test_health_carries_the_eviction_counts_as_integers_only():
    with patch("broker_engine.health.requests.get", side_effect=_answering):
        r = TestClient(main.app).get("/health")
    assert r.status_code == 200
    assert r.json()["evictions"] == ZERO_EVICTIONS


def test_evictions_are_counted_per_table_persist_across_a_reopen_and_name_no_intent(monkeypatch):
    monkeypatch.setenv("BROKER_OUTCOME_CAP", "1")
    reset_store()
    store = get_store()
    store.record_outcome("intent-secret-a", "success")
    store.record_outcome("intent-secret-b", "failure")
    web = TestClient(main.app)
    with patch("broker_engine.health.requests.get", side_effect=_answering):
        first = web.get("/health")
        assert first.json()["evictions"] == {"outcomes": 1, "marks": 0, "situations": 0, "events": 0}
        assert "intent-secret" not in first.text
        reset_store()
        health._reset_cache()
        again = web.get("/health")
    assert again.json()["evictions"] == {"outcomes": 1, "marks": 0, "situations": 0, "events": 0}
    assert "intent-secret" not in again.text


def test_evictions_are_null_when_the_store_does_not_answer():
    with patch.object(StateStore, "ping", side_effect=StateUnavailable()), \
            patch("broker_engine.health.requests.get", side_effect=_answering):
        r = TestClient(main.app).get("/health")
    assert r.status_code == 503
    assert r.json()["evictions"] is None and r.json()["status"] == "degraded"


def test_counters_that_fail_to_read_make_the_state_not_answering():
    with patch.object(StateStore, "evictions", side_effect=StateUnavailable()), \
            patch("broker_engine.health.requests.get", side_effect=_answering):
        r = TestClient(main.app).get("/health")
    assert r.status_code == 503
    assert r.json()["state"] == "not answering" and r.json()["evictions"] is None


def test_the_counters_are_read_once_per_window():
    real = StateStore.evictions
    reads = []

    def spy(self):
        reads.append(1)
        return real(self)

    web = TestClient(main.app)
    with patch.object(StateStore, "evictions", autospec=True, side_effect=spy), \
            patch("broker_engine.health.requests.get", side_effect=_answering):
        for _ in range(20):
            web.get("/health")
    assert len(reads) == 1


# ---- a store failure is the same fixed 503 on every route that touches it (D-04) ----

UNAVAILABLE = {"detail": STATE_UNAVAILABLE}


class Canary(StateUnavailable):
    """A store failure whose own text carries a marker that must never leave the process."""

    def __init__(self):
        Exception.__init__(self, "CANARY /secret/path")
LATENCY = "http://broadband-forum.org/Intent#LatencyMetric"
ASSESS_BODY = {"intent_id": "hsi-demo-1", "time_window": "5m"}


def _assess_token():
    return token(scopes=SCOPES)


def _fresh_join(device, interface, metric, window, **kw):
    seen = datetime.now(timezone.utc) - timedelta(seconds=10)
    return build_observed(f"{device}:{interface}", 42.0, "victoriametrics", seen,
                          datetime.now(timezone.utc))


@pytest.fixture
def tmf921(monkeypatch):
    """The assess route over a pinned binding, the intent server spied and a fresh sample."""
    binding = IntentBinding("hsi-demo-1", {LATENCY: ("olt1", "pon0/1")}, {LATENCY: 100.0})
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding", lambda i: binding)
    monkeypatch.setattr("broker_engine.main.join_observed_metric", _fresh_join)
    with patch("intent_management.tmf921_client.Tmf921Client.get_intent",
               return_value=tmf921_intent((LATENCY, 100))) as get_intent, \
            patch("intent_management.tmf921_client.Tmf921Client.post_observation") as push, \
            patch("intent_management.tmf921_client.Tmf921Client.list_reports", return_value=[]):
        yield MagicMock(get_intent=get_intent, push=push)


def _assess():
    return client.post("/context/assess", json=ASSESS_BODY, headers=_assess_token())


def _only_the_fixed_503(r, caplog):
    assert r.status_code == 503, r.text
    assert r.json() == UNAVAILABLE
    assert "intent server unavailable" not in r.text and "intent server unavailable" not in caplog.text
    assert "CANARY" not in r.text and "CANARY" not in caplog.text and "/secret/path" not in caplog.text


def test_503_assess_when_the_last_outcome_cannot_be_read(tmf921, caplog):
    with patch.object(StateStore, "last_outcome", side_effect=Canary()), \
            caplog.at_level(logging.WARNING):
        r = _assess()
    _only_the_fixed_503(r, caplog)
    tmf921.get_intent.assert_not_called()
    tmf921.push.assert_not_called()


def test_503_assess_when_the_marks_cannot_be_read_after_the_goal_check(tmf921, caplog):
    with patch.object(StateStore, "marks", side_effect=Canary()), \
            caplog.at_level(logging.WARNING):
        r = _assess()
    _only_the_fixed_503(r, caplog)
    assert tmf921.get_intent.call_count == 1  # the goal check ran; nothing was pushed
    tmf921.push.assert_not_called()


def test_503_context_request_when_the_last_outcome_cannot_be_read(olt, caplog):
    bind(olt.fx, "i1", LAT, 100.0, IF0, T0)
    sample(olt)
    live_goal(olt)
    with patch.object(StateStore, "last_outcome", side_effect=Canary()), \
            caplog.at_level(logging.WARNING):
        r = assure()
    _only_the_fixed_503(r, caplog)
    assert get_store().situation_count() == 0


def test_503_context_request_when_the_situation_cannot_be_recorded(rig, caplog):
    with patch.object(StateStore, "record_situation", side_effect=Canary()), \
            caplog.at_level(logging.WARNING):
        r = client.post("/context/request", headers=token(), json=TOPOLOGY)
    _only_the_fixed_503(r, caplog)


def test_503_situation_fetch_when_the_store_cannot_be_read(rig, caplog):
    sid = recorded(rig)["situation"]["id"]
    with patch.object(StateStore, "get_situation", side_effect=Canary()), \
            caplog.at_level(logging.WARNING):
        r = fetch(sid)
    _only_the_fixed_503(r, caplog)


def test_a_closed_store_is_the_same_unavailable_503_on_the_fetch(rig, caplog):
    sid = recorded(rig)["situation"]["id"]
    get_store().close()  # later use is a sqlite error, which the store turns into StateUnavailable
    with caplog.at_level(logging.WARNING):
        r = fetch(sid)
    reset_store()  # the rig's teardown reads the marks, so it needs an open store
    _only_the_fixed_503(r, caplog)
