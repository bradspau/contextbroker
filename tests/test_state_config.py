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


# -- caps (Task 2) ---------------------------------------------------------

CAP_VARS = ["BROKER_OUTCOME_CAP", "BROKER_MARK_CAP", "BROKER_SITUATION_CAP"]


def test_cap_defaults():
    s = load_settings({})
    assert (s.outcome_cap, s.mark_cap, s.situation_cap) == (1024, 1024, 100000)


@pytest.mark.parametrize("name", CAP_VARS)
@pytest.mark.parametrize("bad", ["0", "-1", "1000001", "abc", "1e3", " 5", "canary99x", "5\n"])
def test_bad_cap_is_refused_naming_the_variable_only(name, bad):
    with pytest.raises(ValueError) as info:
        load_settings({name: bad})
    assert str(info.value) == f"{name} must be a whole number from 1 to 1000000"
    assert "canary99x" not in str(info.value)


@pytest.mark.parametrize("name", CAP_VARS)
@pytest.mark.parametrize("good", ["1", "1000000"])
def test_cap_limits_are_accepted(name, good):
    load_settings({name: good})


def test_get_store_passes_the_caps(monkeypatch):
    monkeypatch.setenv("BROKER_OUTCOME_CAP", "3")
    monkeypatch.setenv("BROKER_MARK_CAP", "4")
    monkeypatch.setenv("BROKER_SITUATION_CAP", "5")
    reset_store()
    assert get_store()._caps == {"outcomes": 3, "marks": 4, "situations": 5,
                                  "events": 100000}


# -- situation retention (Task 3) ----------------------------------------------

RETENTION_MSG = ("BROKER_SITUATION_RETENTION must be digits then h or d, "
                 "from 1h to 90d, for example 7d")


def test_retention_default_and_limits():
    assert load_settings({}).situation_retention_s == 604800
    assert load_settings({"BROKER_SITUATION_RETENTION": ""}).situation_retention_s == 604800
    assert load_settings({"BROKER_SITUATION_RETENTION": "1h"}).situation_retention_s == 3600
    assert load_settings({"BROKER_SITUATION_RETENTION": "90d"}).situation_retention_s == 90 * 86400


@pytest.mark.parametrize("bad", ["0h", "59m", "91d", "7", "7w", "d", "7 d", "07x", "canary99d",
                                 "89h\n", "2161h"])
def test_bad_retention_is_refused_with_the_fixed_message(bad):
    with pytest.raises(ValueError) as info:
        load_settings({"BROKER_SITUATION_RETENTION": bad})
    assert str(info.value) == RETENTION_MSG
    assert "canary99d" not in str(info.value)


def test_get_store_passes_the_retention(monkeypatch):
    monkeypatch.setenv("BROKER_SITUATION_RETENTION", "2h")
    reset_store()
    assert get_store()._retention_s == 7200
