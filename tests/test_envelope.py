import json
from datetime import datetime, timedelta, timezone

import pytest

from data_contracts.envelope import build_observed

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_build_observed_marks_stale():
    env = build_observed("e1", 1.0, "src", NOW - timedelta(minutes=10), NOW, 300)
    assert env.quality.stale is True


def test_build_observed_fresh():
    env = build_observed("e1", 1.0, "src", NOW - timedelta(seconds=10), NOW, 300)
    assert env.quality.stale is False
    assert env.quality.missing is False


def test_none_value_marks_missing():
    env = build_observed("e1", None, "src", None, NOW, 300)
    assert env.quality.missing is True
    assert env.quality.stale is False
    assert env.observed_at is None


def test_to_dict_is_json_serialisable():
    env = build_observed("e1", 1.0, "src", NOW, NOW, 300)
    d = json.loads(json.dumps(env.to_dict()))
    assert d["state"] == "observed"
    assert d["observed_at"] == NOW.isoformat()


def test_present_value_without_timestamp_is_stale():
    env = build_observed("e1", 1.0, "src", None, NOW, 300)
    assert env.quality.stale is True
    assert env.quality.missing is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_value_is_missing(bad):
    env = build_observed("e1", bad, "src", NOW, NOW, 300)
    assert env.value is None
    assert env.quality.missing is True
    assert env.quality.stale is False
    assert env.observed_at == NOW


@pytest.mark.parametrize("ok", [0.0, 1, -3.5, "text"])
def test_other_values_unchanged(ok):
    env = build_observed("e1", ok, "src", NOW, NOW, 300)
    assert env.value == ok
    assert env.quality.missing is False
    assert env.quality.stale is False


def test_none_value_unchanged():
    env = build_observed("e1", None, "src", None, NOW, 300)
    assert env.value is None and env.quality.missing is True
