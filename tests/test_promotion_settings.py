"""The promoted-store settings and the two promotion thresholds (plan 07-03, D-57, D-66)."""
import re

import pytest
import yaml

from broker_state.process import get_store, load_settings, promotion_thresholds, reset_store
from tests.test_docker_compose import COMPOSE_PATH, ENV_EXAMPLE_PATH, _env_as_dict

PROMOTE_KEYS = ("BROKER_PROMOTED_CAP", "BROKER_PROMOTED_RETENTION",
                "BROKER_PROMOTE_MIN_EVENTS", "BROKER_PROMOTE_WINDOW")
FIELDS = ("promoted_cap", "promoted_retention_s", "promote_min_events", "promote_window_s")


def test_defaults():
    s = load_settings({})
    assert (s.promoted_cap, s.promoted_retention_s, s.promote_min_events,
            s.promote_window_s) == (10000, 2592000, 3, 900)
    assert promotion_thresholds({}) == (3, 900)


def test_valid_values_are_read():
    s = load_settings({"BROKER_PROMOTED_CAP": "50", "BROKER_PROMOTED_RETENTION": "2d",
                       "BROKER_PROMOTE_MIN_EVENTS": "5", "BROKER_PROMOTE_WINDOW": "30m"})
    assert (s.promoted_cap, s.promoted_retention_s, s.promote_min_events,
            s.promote_window_s) == (50, 172800, 5, 1800)
    assert promotion_thresholds({"BROKER_PROMOTE_MIN_EVENTS": "2",
                                 "BROKER_PROMOTE_WINDOW": "1m"}) == (2, 60)
    assert promotion_thresholds({"BROKER_PROMOTE_MIN_EVENTS": "1000"})[0] == 1000


@pytest.mark.parametrize("name, bad", [
    ("BROKER_PROMOTED_CAP", "0"),
    ("BROKER_PROMOTED_CAP", "x"),
    ("BROKER_PROMOTED_CAP", "1000001"),
    ("BROKER_PROMOTED_RETENTION", "30"),
    ("BROKER_PROMOTED_RETENTION", "0d"),
    ("BROKER_PROMOTED_RETENTION", "91d"),
    ("BROKER_PROMOTE_MIN_EVENTS", "1"),
    ("BROKER_PROMOTE_MIN_EVENTS", "1001"),
    ("BROKER_PROMOTE_MIN_EVENTS", "-3"),
    ("BROKER_PROMOTE_MIN_EVENTS", "3.5"),
    ("BROKER_PROMOTE_WINDOW", "30s"),
    ("BROKER_PROMOTE_WINDOW", "8d"),
    ("BROKER_PROMOTE_WINDOW", "15"),
])
def test_a_bad_value_is_refused_naming_the_variable_and_never_the_value(name, bad):
    with pytest.raises(ValueError) as info:
        load_settings({name: bad})
    assert name in str(info.value)


@pytest.mark.parametrize("name, bad", [
    ("BROKER_PROMOTED_CAP", "canary99x"),
    ("BROKER_PROMOTED_RETENTION", "canary99x"),
    ("BROKER_PROMOTE_MIN_EVENTS", "canary99x"),
    ("BROKER_PROMOTE_WINDOW", "canary99x"),
])
def test_a_distinctive_bad_value_never_appears_in_the_message(name, bad):
    with pytest.raises(ValueError) as info:
        load_settings({name: bad})
    assert name in str(info.value) and bad not in str(info.value)


def test_the_promote_min_events_message_is_fixed():
    with pytest.raises(ValueError) as info:
        promotion_thresholds({"BROKER_PROMOTE_MIN_EVENTS": "1"})
    assert str(info.value) == "BROKER_PROMOTE_MIN_EVENTS must be a whole number from 2 to 1000"


def test_the_promote_window_may_not_exceed_the_event_retention():
    env = {"BROKER_PROMOTE_WINDOW": "2d", "BROKER_EVENT_RETENTION": "1d"}
    with pytest.raises(ValueError) as info:
        load_settings(env)
    assert "BROKER_PROMOTE_WINDOW" in str(info.value)
    assert "1d" not in str(info.value) and "2d" not in str(info.value)
    # at the retention it is fine
    assert load_settings({"BROKER_PROMOTE_WINDOW": "1d",
                          "BROKER_EVENT_RETENTION": "1d"}).promote_window_s == 86400


def test_get_store_passes_the_promoted_settings(monkeypatch):
    monkeypatch.setenv("BROKER_PROMOTED_RETENTION", "2d")
    monkeypatch.setenv("BROKER_PROMOTED_CAP", "7")
    reset_store()
    store = get_store()
    assert store.promoted_retention_s == 172800
    assert store._promoted_cap == 7
    reset_store()


def test_compose_passes_the_four_settings_once_each_to_the_api_only():
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    api_env = services["contextbroker-api"]["environment"]
    for item in ("BROKER_PROMOTED_CAP=${BROKER_PROMOTED_CAP:-10000}",
                 "BROKER_PROMOTED_RETENTION=${BROKER_PROMOTED_RETENTION:-30d}",
                 "BROKER_PROMOTE_MIN_EVENTS=${BROKER_PROMOTE_MIN_EVENTS:-3}",
                 "BROKER_PROMOTE_WINDOW=${BROKER_PROMOTE_WINDOW:-15m}"):
        assert api_env.count(item) == 1, item
    env = _env_as_dict(api_env)
    assert all(key in env for key in PROMOTE_KEYS)
    for name, svc in services.items():
        if name == "contextbroker-api":
            continue
        for key in PROMOTE_KEYS:
            assert key not in repr(svc), (name, key)


def test_compose_defaults_equal_the_code_defaults():
    api_env = yaml.safe_load(COMPOSE_PATH.read_text())["services"]["contextbroker-api"]["environment"]
    pattern = re.compile(r"^(BROKER_PROMOTE[D]?_[A-Z_]+)=\$\{\1:-([^}]*)\}$")
    defaults = {}
    for item in api_env:
        match = pattern.match(item)
        if match:
            defaults[match.group(1)] = match.group(2)
    assert set(defaults) == set(PROMOTE_KEYS)
    from_compose = load_settings(defaults)
    from_code = load_settings({})
    for field in FIELDS:
        assert getattr(from_compose, field) == getattr(from_code, field), field


def test_env_example_documents_each_setting_with_a_limit_line_and_no_live_assignment():
    lines = ENV_EXAMPLE_PATH.read_text().splitlines()
    block_end = lines.index("# TMF921 server (compose profile tmf921)")
    block_start = next(i for i, line in enumerate(lines) if line.startswith("# Broker state."))
    for key in PROMOTE_KEYS:
        assert not any(line.startswith(key + "=") for line in lines), key
        indexes = [i for i, line in enumerate(lines) if line.startswith(f"# {key}=")]
        assert len(indexes) == 1, key
        i = indexes[0]
        assert lines[i - 1].startswith("# ") and not re.match(r"# BROKER_[A-Z_]+=", lines[i - 1]), key
        assert " to " in lines[i - 1], key
        assert block_start < i < block_end, key
