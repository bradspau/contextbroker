"""State settings from the environment, and the process handle."""
from datetime import datetime, timedelta, timezone

import pytest

from broker_state import process
from broker_state.process import (DEFAULT_PATH, ENV_PATH, get_store, load_settings,
                                  open_process_store, reset_store)

T = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
SID = "sit-" + "ab" * 16


def test_path_default_and_empty():
    assert load_settings({}).path == DEFAULT_PATH == "/data/broker-state.sqlite3"
    assert load_settings({ENV_PATH: ""}).path == DEFAULT_PATH


def test_memory_path_is_refused_naming_the_variable():
    with pytest.raises(ValueError) as info:
        load_settings({ENV_PATH: ":memory:"})
    assert "BROKER_STATE_PATH" in str(info.value)


def test_get_store_is_one_per_process_and_reset_reopens_the_same_file():
    first = get_store()
    assert get_store() is first
    assert open_process_store() is first
    first.record_situation(SID, '{"k": 1}', expires_at=T + timedelta(seconds=60), recorded_at=T)
    reset_store()
    second = get_store()
    assert second is not first
    assert second.get_situation(SID, now=T).body == '{"k": 1}'


def test_get_store_opens_the_file_named_by_the_environment(tmp_path, monkeypatch):
    target = tmp_path / "named.sqlite3"
    monkeypatch.setenv(ENV_PATH, str(target))
    reset_store()
    get_store()
    assert target.exists()
    assert process._STORE is not None
