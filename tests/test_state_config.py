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


# -- event settings (plan 05-01, Task 3) ----------------------------------------

from broker_state.process import event_window_limits  # noqa: E402

EVENT_RETENTION_MSG = ("BROKER_EVENT_RETENTION must be digits then h or d, "
                       "from 1h to 90d, for example 7d")


def window_msg(name):
    return f"{name} must be digits then s, m, h or d, from 1m to 7d, for example 15m"


def test_event_setting_defaults():
    s = load_settings({})
    assert (s.event_cap, s.event_retention_s, s.event_window_default_s,
            s.event_window_max_s) == (100000, 604800, 900, 86400)
    assert event_window_limits({}) == (900, 86400)
    assert load_settings({"BROKER_EVENT_CAP": "", "BROKER_EVENT_RETENTION": "",
                          "BROKER_EVENT_WINDOW_DEFAULT": "",
                          "BROKER_EVENT_WINDOW_MAX": ""}).event_cap == 100000


@pytest.mark.parametrize("bad", ["0", "1000001", "abc", " 5", "-1", "1e3", "canary99d"])
def test_bad_event_cap_is_refused_naming_the_variable_only(bad):
    with pytest.raises(ValueError) as info:
        load_settings({"BROKER_EVENT_CAP": bad})
    assert str(info.value) == "BROKER_EVENT_CAP must be a whole number from 1 to 1000000"
    assert "canary99d" not in str(info.value)


@pytest.mark.parametrize("good", ["1", "1000000"])
def test_event_cap_limits_are_accepted(good):
    assert load_settings({"BROKER_EVENT_CAP": good}).event_cap == int(good)


@pytest.mark.parametrize("bad", ["59m", "91d", "7", "7 d", "0h", "7w", "canary99d", "5h\n",
                                 "9" * 5000 + "d"])
def test_bad_event_retention_is_refused_with_the_fixed_message(bad):
    with pytest.raises(ValueError) as info:
        load_settings({"BROKER_EVENT_RETENTION": bad})
    assert str(info.value) == EVENT_RETENTION_MSG
    assert "canary99d" not in str(info.value)


def test_event_retention_limits():
    assert load_settings({"BROKER_EVENT_RETENTION": "1h",
                          "BROKER_EVENT_WINDOW_MAX": "1h"}).event_retention_s == 3600
    assert load_settings({"BROKER_EVENT_RETENTION": "90d"}).event_retention_s == 90 * 86400


@pytest.mark.parametrize("name", ["BROKER_EVENT_WINDOW_DEFAULT", "BROKER_EVENT_WINDOW_MAX"])
@pytest.mark.parametrize("bad", ["30s", "8d", "15", "1w", "0m", "m", "15 m", "canary99d",
                                 "5m\n", "9" * 5000 + "s"])
def test_bad_window_is_refused_with_the_fixed_message(name, bad):
    with pytest.raises(ValueError) as info:
        load_settings({name: bad})
    assert str(info.value) == window_msg(name)
    with pytest.raises(ValueError) as info2:
        event_window_limits({name: bad})
    assert str(info2.value) == window_msg(name)
    assert "canary99d" not in str(info.value)


def test_window_limits_are_accepted():
    s = load_settings({"BROKER_EVENT_WINDOW_DEFAULT": "1m", "BROKER_EVENT_WINDOW_MAX": "7d"})
    assert (s.event_window_default_s, s.event_window_max_s) == (60, 604800)
    assert event_window_limits({"BROKER_EVENT_WINDOW_DEFAULT": "90s",
                                "BROKER_EVENT_WINDOW_MAX": "2h"}) == (90, 7200)


def test_default_window_above_the_maximum_is_refused():
    env = {"BROKER_EVENT_WINDOW_DEFAULT": "2h", "BROKER_EVENT_WINDOW_MAX": "1h"}
    for call in (load_settings, event_window_limits):
        with pytest.raises(ValueError) as info:
            call(env)
        assert str(info.value) == ("BROKER_EVENT_WINDOW_DEFAULT must not exceed "
                                   "BROKER_EVENT_WINDOW_MAX")


def test_maximum_window_above_the_retention_is_refused():
    env = {"BROKER_EVENT_WINDOW_MAX": "2d", "BROKER_EVENT_RETENTION": "1d"}
    for call in (load_settings, event_window_limits):
        with pytest.raises(ValueError) as info:
            call(env)
        assert str(info.value) == ("BROKER_EVENT_WINDOW_MAX must not exceed "
                                   "BROKER_EVENT_RETENTION")


def test_maximum_window_equal_to_the_retention_is_accepted():
    assert event_window_limits({"BROKER_EVENT_WINDOW_MAX": "1d",
                                "BROKER_EVENT_RETENTION": "1d"}) == (900, 86400)


def test_situation_retention_message_is_unchanged():
    with pytest.raises(ValueError) as info:
        load_settings({"BROKER_SITUATION_RETENTION": "7"})
    assert str(info.value) == RETENTION_MSG


def test_get_store_passes_the_event_settings(monkeypatch):
    monkeypatch.setenv("BROKER_EVENT_CAP", "7")
    monkeypatch.setenv("BROKER_EVENT_RETENTION", "2h")
    monkeypatch.setenv("BROKER_EVENT_WINDOW_MAX", "1h")
    reset_store()
    store = get_store()
    assert store._caps["events"] == 7
    assert store.event_retention_s == 7200


def test_bad_event_setting_stops_get_store(monkeypatch):
    monkeypatch.setenv("BROKER_EVENT_WINDOW_DEFAULT", "30s")
    reset_store()
    with pytest.raises(ValueError):
        get_store()
