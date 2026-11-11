import json
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest

from broker_engine.tsdb_joiner import join_observed_metric

TS = 1_700_000_000.5


def _resp(payload):
    r = Mock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def test_join_observed_metric_returns_envelope():
    r = _resp({"data": {"result": [{"value": [TS + 100, "3"]}]}})
    r2 = _resp({"data": {"result": [{"value": [TS + 100, str(TS)]}]}})
    now = datetime.fromtimestamp(TS, timezone.utc)
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]) as g:
        env = join_observed_metric("r1", "eth0", "utilization", "5m", now=now)
    assert env.value == 3.0
    assert env.entity_id == "r1:eth0"
    assert env.state.value == "observed"
    assert env.source == "victoriametrics"
    assert env.observed_at == datetime.fromtimestamp(TS, timezone.utc)
    assert not env.quality.missing and not env.quality.stale
    assert g.call_args_list[0].kwargs["params"]["query"].startswith("max_over_time(")
    assert g.call_args_list[1].kwargs["params"]["query"].startswith("min(tlast_over_time(")


def test_join_observed_metric_stale():
    now = datetime.fromtimestamp(TS, timezone.utc)
    r = _resp({"data": {"result": [{"value": [TS, "3"]}]}})
    r2 = _resp({"data": {"result": [{"value": [TS, str(TS - 600)]}]}})
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]):
        env = join_observed_metric("r1", "eth0", "utilization", "15m", now=now)
    assert env.quality.stale is True


def test_join_observed_metric_fails_closed_without_timestamp():
    now = datetime.fromtimestamp(TS, timezone.utc)
    r = _resp({"data": {"result": [{"value": [TS, "3"]}]}})
    for second in (_resp({"data": {"result": []}}), RuntimeError("boom"),
                   _resp({"data": {"result": [{"value": [TS, "nan-ish"]}]}})):
        with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, second]):
            env = join_observed_metric("r1", "eth0", "utilization", "5m", now=now)
        assert env.value == 3.0
        assert env.observed_at is None
        assert env.quality.stale is True and not env.quality.missing


@pytest.mark.parametrize("bad_ts", ["NaN", "inf", "-inf", "1e20", "-5", "0"])
def test_join_observed_metric_non_finite_or_out_of_range_timestamp_is_stale(bad_ts):
    # New-1: these used to raise ValueError/OverflowError out of the joiner.
    now = datetime.fromtimestamp(TS, timezone.utc)
    r = _resp({"data": {"result": [{"value": [TS, "3"]}]}})
    r2 = _resp({"data": {"result": [{"value": [TS, bad_ts]}]}})
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]):
        env = join_observed_metric("r1", "eth0", "utilization", "5m", now=now)
    assert env.observed_at is None
    assert env.quality.stale is True and env.value == 3.0


def test_join_observed_metric_future_timestamp_is_stale():
    now = datetime.fromtimestamp(TS, timezone.utc)
    r = _resp({"data": {"result": [{"value": [TS, "3"]}]}})
    r2 = _resp({"data": {"result": [{"value": [TS, str(TS + 86400)]}]}})
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]):
        env = join_observed_metric("r1", "eth0", "utilization", "5m", now=now)
    assert env.quality.stale is True


def test_join_observed_metric_multiple_series_flagged_as_conflict():
    # New-3: the right worst case depends on the operator, so >1 series must not
    # silently collapse to one value.
    now = datetime.fromtimestamp(TS, timezone.utc)
    r = _resp({"data": {"result": [
        {"metric": {"job": "a"}, "value": [TS, "500"]},
        {"metric": {"job": "b"}, "value": [TS, "10"]},
    ]}})
    r2 = _resp({"data": {"result": [{"value": [TS, str(TS)]}]}})
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]):
        env = join_observed_metric("r1", "eth0", "utilization", "5m", now=now)
    assert env.quality.conflicts == ["multiple series match selector (2)"]


def test_join_observed_metric_single_series_has_no_conflict():
    now = datetime.fromtimestamp(TS, timezone.utc)
    r = _resp({"data": {"result": [{"value": [TS, "3"]}]}})
    r2 = _resp({"data": {"result": [{"value": [TS, str(TS)]}]}})
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]):
        env = join_observed_metric("r1", "eth0", "utilization", "5m", now=now)
    assert env.quality.conflicts == []


def test_join_observed_metric_live_response_shape():
    # Shapes captured from VictoriaMetrics v1.102.0 (aggregated, empty metric labels).
    now = datetime.fromtimestamp(1793009347, timezone.utc)
    r = _resp({"status": "success", "data": {"resultType": "vector",
               "result": [{"metric": {}, "value": [1793009347, "9"]}]}})
    r2 = _resp({"status": "success", "data": {"resultType": "vector",
                "result": [{"metric": {}, "value": [1793009347, "1793008245"]}]}})
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]):
        env = join_observed_metric("r1", "old", "input_drops", "30m", now=now)
    assert env.value == 9.0
    assert env.observed_at == datetime.fromtimestamp(1793008245, timezone.utc)
    assert env.quality.stale is True


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


def test_graph_intent_resolver_and_not_found_are_gone():
    import importlib

    import broker_engine.sparql_resolver as sparql_resolver
    import broker_engine.validation as validation
    assert not hasattr(sparql_resolver, "resolve_intent")
    assert not hasattr(sparql_resolver, "_build_intent_sparql")
    assert hasattr(sparql_resolver, "FUSEKI_QUERY_ENDPOINT")
    assert not hasattr(validation, "IntentNotFound")
    for name in ("broker_engine.main", "broker_engine.assess", "broker_engine.operations",
                 "broker_engine.binding_resolver", "agent_harness.tools", "broker",
                 "ingestion.bind_intent", "intent_management.tmf921_source"):
        importlib.import_module(name)


def test_safe_token_and_window_reject_trailing_newline():
    import pytest
    from broker_engine.validation import _validate_token, SAFE_WINDOW, ClientInputError
    with pytest.raises(ClientInputError):
        _validate_token("input_drops\n", "metric")
    assert not SAFE_WINDOW.match("30m\n")


def _join_query(**kw):
    r = _resp({"data": {"result": [{"value": [TS + 100, "3"]}]}})
    r2 = _resp({"data": {"result": [{"value": [TS + 100, str(TS)]}]}})
    now = datetime.fromtimestamp(TS, timezone.utc)
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]) as g:
        join_observed_metric("r1", "eth0", "utilization", "5m", now=now, **kw)
    return [c.kwargs["params"]["query"] for c in g.call_args_list]


def test_join_default_is_max_over_time():
    q = _join_query()
    assert q[0].startswith("max_over_time(") and q[1].startswith("min(tlast_over_time(")


def test_join_aggregate_last_uses_last_over_time():
    q = _join_query(aggregate="last")
    assert q[0].startswith("last_over_time(") and q[1].startswith("min(tlast_over_time(")


def test_join_aggregate_validated():
    from broker_engine.validation import ClientInputError
    with pytest.raises(ClientInputError):
        join_observed_metric("r1", "eth0", "utilization", "5m", aggregate="x}")


def _record_of(env):
    from data_contracts.envelope import ProvenanceRecord
    assert len(env.provenance) == 1
    rec = env.provenance[0]
    assert isinstance(rec, ProvenanceRecord)
    return rec


def test_joined_envelope_carries_one_provenance_record():
    from data_contracts.envelope import ProvenanceRecord
    now = datetime.fromtimestamp(TS, timezone.utc)
    r = _resp({"data": {"result": [{"value": [TS, "3"]}]}})
    r2 = _resp({"data": {"result": [{"value": [TS, str(TS)]}]}})
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]):
        env = join_observed_metric("r1", "eth0", "latency_ms", "5m", now=now)
    assert env.provenance == [ProvenanceRecord(
        "victoriametrics", "interface_metrics_latency_ms", env.observed_at, now)]
    assert env.provenance[0].observed_at is not None
    assert env.to_dict()["provenance"] == [{
        "source_system": "victoriametrics",
        "reference": "interface_metrics_latency_ms",
        "observed_at": env.observed_at.isoformat(),
        "retrieved_at": now.isoformat(),
    }]


def test_joined_provenance_retrieved_at_is_callers_now_when_as_of_shifts_query():
    from datetime import timedelta
    now = datetime.fromtimestamp(TS, timezone.utc)
    past = now - timedelta(minutes=10)
    r = _resp({"data": {"result": [{"value": [TS, "3"]}]}})
    r2 = _resp({"data": {"result": [{"value": [TS, str(past.timestamp())]}]}})
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]) as g:
        env = join_observed_metric("r1", "eth0", "latency_ms", "5m", now=now, as_of="10m")
    assert g.call_args_list[0].kwargs["params"]["time"] == past.timestamp()
    rec = _record_of(env)
    assert rec.retrieved_at == now
    assert rec.observed_at == env.observed_at == past


def test_missing_and_unreadable_answers_carry_one_record_without_observation_time():
    now = datetime.fromtimestamp(TS, timezone.utc)
    answers = [
        _resp({"data": {"result": []}}),
        _resp({"data": {"result": [{"value": [TS, "NaN"]}]}}),
        _resp({"data": {"result": [{"value": [TS, "not-a-number"]}]}}),
    ]
    for answer in answers:
        with patch("broker_engine.tsdb_joiner.requests.get", return_value=answer):
            env = join_observed_metric("r1", "eth0", "latency_ms", "5m", now=now)
        assert env.quality.missing
        rec = _record_of(env)
        assert rec.source_system == "victoriametrics"
        assert rec.reference == "interface_metrics_latency_ms"
        assert rec.observed_at is None
        assert rec.retrieved_at == now


def test_multiple_series_keep_conflict_and_carry_one_record():
    now = datetime.fromtimestamp(TS, timezone.utc)
    r = _resp({"data": {"result": [
        {"metric": {"job": "a"}, "value": [TS, "500"]},
        {"metric": {"job": "b"}, "value": [TS, "10"]},
    ]}})
    r2 = _resp({"data": {"result": [{"value": [TS, str(TS)]}]}})
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]):
        env = join_observed_metric("r1", "eth0", "latency_ms", "5m", now=now)
    assert env.quality.conflicts == ["multiple series match selector (2)"]
    assert _record_of(env).reference == "interface_metrics_latency_ms"


def test_provenance_reference_comes_from_pinned_names_not_the_answer():
    now = datetime.fromtimestamp(TS, timezone.utc)
    r = _resp({"data": {"result": [{"metric": {"__name__": "evil text"}, "value": [TS, "3"]}]}})
    r2 = _resp({"data": {"result": [{"value": [TS, str(TS)]}]}})
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]):
        env = join_observed_metric("r1", "eth0", "utilization", "5m", now=now)
    assert "evil" not in json.dumps(env.to_dict())
