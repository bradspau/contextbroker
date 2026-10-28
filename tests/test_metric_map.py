import json
import pytest
from data_contracts.metric_map import load_metric_map, broker_metric_for

URI = "http://broadband-forum.org/Intent#LatencyMetric"


def _w(tmp_path, data):
    p = tmp_path / "m.json"
    p.write_text(json.dumps(data))
    return p


def _e(**kw):
    d = {"broker_metric": "latency_ms", "metric_uri": URI, "unit": "ms", "operator": "<="}
    d.update(kw)
    return d


def test_load_metric_map_ok():
    m = load_metric_map()
    assert len(m) == 5
    assert m[URI].broker_metric == "latency_ms" and m[URI].unit == "ms"
    # Verified against the live TMF921 server on 2026-10-28 (tests/test_tmf921_live.py):
    # quan:smaller is strict, so a value exactly at the bound fails.
    ops = {v.broker_metric: v.operator for v in m.values()}
    assert ops == {"latency_ms": "<", "jitter_ms": "<", "packet_loss_percent": "<",
                   "downstream_bandwidth_mbps": ">=", "upstream_bandwidth_mbps": ">="}


def test_unknown_unit_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_metric_map(_w(tmp_path, [_e(unit="parsecs")]))


def test_unsafe_uri_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_metric_map(_w(tmp_path, [_e(metric_uri="http://x/a b\"")]))


def test_unsafe_broker_metric_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_metric_map(_w(tmp_path, [_e(broker_metric="a;b")]))


def test_malformed_shape_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_metric_map(_w(tmp_path, {"a": 1}))
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(ValueError):
        load_metric_map(p)


def test_missing_file_empty(tmp_path):
    assert load_metric_map(tmp_path / "nope.json") == {}


def test_broker_metric_for_unknown_returns_none():
    assert broker_metric_for("http://example.org/Unknown") is None


@pytest.mark.parametrize("field,val", [
    ("metric_uri", URI + "\n"), ("broker_metric", "latency_ms\n"),
    ("metric_uri", "http://x/a\n"),
])
def test_trailing_newline_rejected(tmp_path, field, val):
    with pytest.raises(ValueError):
        load_metric_map(_w(tmp_path, [_e(**{field: val})]))


def test_duplicate_metric_uri_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_metric_map(_w(tmp_path, [_e(), _e(broker_metric="other")]))


def test_duplicate_broker_metric_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_metric_map(_w(tmp_path, [_e(), _e(metric_uri="http://x.org/B")]))


@pytest.mark.parametrize("unit", [5, ["ms"]])
def test_unit_wrong_type_rejected(tmp_path, unit):
    with pytest.raises(ValueError):
        load_metric_map(_w(tmp_path, [_e(unit=unit)]))


@pytest.mark.parametrize("unit", ["Mbps", "Gbps", "ms", "us", "percent", None])
def test_accepted_units(tmp_path, unit):
    assert load_metric_map(_w(tmp_path, [_e(unit=unit)]))[URI].unit == unit


def test_operator_required(tmp_path):
    entry = _e()
    del entry["operator"]
    with pytest.raises(ValueError):
        load_metric_map(_w(tmp_path, [entry]))
    with pytest.raises(ValueError):
        load_metric_map(_w(tmp_path, [_e(operator=None)]))


@pytest.mark.parametrize("op", ["=", "!=", "=<", "lt", "<= ", "<=\n", 1, ["<="], ""])
def test_operator_must_be_known(tmp_path, op):
    with pytest.raises(ValueError):
        load_metric_map(_w(tmp_path, [_e(operator=op)]))


@pytest.mark.parametrize("op", ["<", "<=", "==", ">=", ">"])
def test_accepted_operators(tmp_path, op):
    assert load_metric_map(_w(tmp_path, [_e(operator=op)]))[URI].operator == op
