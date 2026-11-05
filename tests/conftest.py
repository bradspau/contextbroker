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
    reset_store()
    yield
    reset_store()
