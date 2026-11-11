import sys
from pathlib import Path

import pytest

# Add contextbroker parent directory to Python path so broker.py can be imported
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _per_test_state_files(tmp_path, monkeypatch):
    """Every test gets its own state file and decision-record file, so none shares
    state with another and none can touch /data."""
    from broker_state.process import reset_store

    monkeypatch.setenv("BROKER_STATE_PATH", str(tmp_path / "broker-state.sqlite3"))
    monkeypatch.setenv("BROKER_DECISION_RECORDS_PATH", str(tmp_path / "decision-records.sqlite3"))
    # No test can probe or write a real Qdrant: the URL points at a closed loopback port.
    monkeypatch.setenv("BROKER_QDRANT_URL", "http://127.0.0.1:1")
    # The real embedder always refuses in tests: the model directory does not exist (D-36).
    monkeypatch.setenv("BROKER_EMBED_MODEL_DIR", str(tmp_path / "no-models"))
    from okf_retrieval.service import reset_service

    reset_store()
    reset_service()
    yield
    reset_service()
    reset_store()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_sessions: use the real per-upstream HTTP sessions, not the shim "
        "that routes broker_engine.upstream_http to requests.get and requests.post",
    )


class _PassThroughSession:
    """What the shim hands out: get and post call requests.get and requests.post
    looked up at call time, so every patch of those functions still intercepts."""

    def get(self, url, **kwargs):
        import requests

        return requests.get(url, **kwargs)

    def post(self, url, **kwargs):
        import requests

        return requests.post(url, **kwargs)


@pytest.fixture(autouse=True)
def _empty_caches():
    """Every test starts with an empty metric map cache and goal-parse cache (D-72): a
    map rewritten in place within one filesystem timestamp tick must not be served stale."""
    from data_contracts.metric_map import clear_metric_map_cache
    from intent_management.tmf921_client import clear_bounds_cache

    clear_metric_map_cache()
    clear_bounds_cache()
    yield


@pytest.fixture(autouse=True)
def _upstream_sessions(request, monkeypatch):
    """Unless a test is marked real_sessions, broker_engine.upstream_http routes to
    requests.get and requests.post (D-87a). Sessions are reset after every test."""
    from broker_engine import upstream_http

    if request.node.get_closest_marker("real_sessions") is None:
        shim = _PassThroughSession()

        def fake_session(upstream):
            if upstream not in upstream_http.UPSTREAMS:
                raise ValueError("unknown upstream")
            return shim

        monkeypatch.setattr(upstream_http, "session", fake_session)
    yield
    upstream_http.reset_sessions()
