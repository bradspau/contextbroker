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
