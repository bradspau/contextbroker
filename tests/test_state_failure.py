"""D-04 and D-12 through the API: no start without a usable state file, /health reports the state store.

Start-up refusal is tested by entering `with TestClient(main.app)`, which runs the lifespan. Other test
files build a TestClient without a with block; they get the lazily opened per-test store instead.
"""
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from broker_engine import health, main
from broker_state.process import get_store, load_settings
from broker_state.store import StateStore, StateUnavailable

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
                                  "BROKER_MARK_CAP", "BROKER_SITUATION_CAP"])
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
