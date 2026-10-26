import json
import pytest
from data_contracts.metric_map import load_metric_map, broker_metric_for

URI = "http://broadband-forum.org/Intent#LatencyMetric"


def _w(tmp_path, data):
    p = tmp_path / "m.json"
    p.write_text(json.dumps(data))
    return p


def _e(**kw):
    d = {"broker_metric": "latency_ms", "metric_uri": URI, "unit": "ms"}
    d.update(kw)
    return d


def test_load_metric_map_ok():
    m = load_metric_map()
    assert len(m) == 5
    assert m[URI].broker_metric == "latency_ms" and m[URI].unit == "ms"


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
