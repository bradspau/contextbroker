import json
from datetime import datetime, timedelta, timezone

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
