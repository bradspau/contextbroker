from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest

from broker_engine.sparql_resolver import resolve_intent
from broker_engine.tsdb_joiner import join_observed_metric

TS = 1_700_000_000.5


def _resp(payload):
    r = Mock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def test_join_observed_metric_returns_envelope():
    r = _resp({"data": {"result": [{"value": [TS, "3"]}]}})
    now = datetime.fromtimestamp(TS, timezone.utc)
    with patch("broker_engine.tsdb_joiner.requests.get", return_value=r):
        env = join_observed_metric("r1", "eth0", "utilization", "5m", now=now)
    assert env.value == 3.0
    assert env.entity_id == "r1:eth0"
    assert env.state.value == "observed"
    assert env.source == "victoriametrics"
    assert env.observed_at == datetime.fromtimestamp(TS, timezone.utc)
    assert not env.quality.missing and not env.quality.stale


def test_join_observed_metric_empty_is_missing():
    r = _resp({"data": {"result": []}})
    with patch("broker_engine.tsdb_joiner.requests.get", return_value=r):
        env = join_observed_metric("r1", "eth0", "utilization", "5m")
    assert env.value is None and env.quality.missing


def test_join_observed_metric_unsafe_token():
    with pytest.raises(ValueError):
        join_observed_metric("r1;drop", "eth0", "utilization", "5m")
    with pytest.raises(ValueError):
        join_observed_metric("r1", "eth0", "utilization", "5 minutes")


def test_resolve_intent_unknown_raises():
    r = _resp({"results": {"bindings": []}})
    with patch("broker_engine.sparql_resolver.requests.post", return_value=r):
        with pytest.raises(ValueError):
            resolve_intent("nope")


def test_resolve_intent_adds_state_and_source():
    b = {"metric": {"value": "utilization"}, "operator": {"value": "lt"},
         "targetValue": {"value": "80"}, "device": {"value": "r1"},
         "interfaceName": {"value": "eth0"}}
    r = _resp({"results": {"bindings": [b]}})
    with patch("broker_engine.sparql_resolver.requests.post", return_value=r):
        out = resolve_intent("intent1")
    assert out["state"] == "intended" and out["source"] == "fuseki"
    assert out["target_value"] == 80.0
    assert out["interfaces"] == [{"device": "r1", "interface_name": "eth0"}]
