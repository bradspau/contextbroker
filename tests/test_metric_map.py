import json
import os
from pathlib import Path

import pytest
from data_contracts.metric_map import (
    broker_metric_for,
    clear_metric_map_cache,
    load_metric_map,
    metric_map_key,
)

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


# ---------- the cache (D-72, PERF-01) ----------
@pytest.fixture
def reads(monkeypatch):
    """Count how many times each file is read from disk."""
    seen: list[str] = []
    real = Path.read_text

    def spy(self, *a, **k):
        seen.append(str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    return seen


def test_ten_calls_on_an_unchanged_file_read_it_once(tmp_path, monkeypatch, reads):
    p = _w(tmp_path, [_e()])
    monkeypatch.setenv("METRIC_MAP_PATH", str(p))
    for _ in range(10):
        assert load_metric_map()[URI].broker_metric == "latency_ms"
    assert reads.count(str(p)) == 1


def test_a_rewritten_file_is_read_again(tmp_path, monkeypatch, reads):
    p = _w(tmp_path, [_e()])
    monkeypatch.setenv("METRIC_MAP_PATH", str(p))
    assert load_metric_map()[URI].operator == "<="
    p.write_text(json.dumps([_e(operator=">=", unit="Mbps")]))  # different size
    assert load_metric_map()[URI].operator == ">="
    assert reads.count(str(p)) == 2


def test_same_size_content_with_a_new_mtime_is_read_again(tmp_path, monkeypatch, reads):
    p = _w(tmp_path, [_e(operator="<")])
    monkeypatch.setenv("METRIC_MAP_PATH", str(p))
    assert load_metric_map()[URI].operator == "<"
    old = p.stat()
    # the same number of bytes, so only the modification time can tell them apart
    p.write_text(json.dumps([_e(operator=">")]))
    assert p.stat().st_size == old.st_size
    os.utime(p, ns=(old.st_atime_ns, old.st_mtime_ns + 5_000_000_000))
    assert load_metric_map()[URI].operator == ">"
    assert reads.count(str(p)) == 2


def test_switching_the_path_returns_the_other_files_map(tmp_path, monkeypatch):
    a = tmp_path / "a.json"
    a.write_text(json.dumps([_e(broker_metric="latency_ms")]))
    b = tmp_path / "b.json"
    b.write_text(json.dumps([_e(broker_metric="jitter_ms")]))
    monkeypatch.setenv("METRIC_MAP_PATH", str(a))
    assert load_metric_map()[URI].broker_metric == "latency_ms"
    monkeypatch.setenv("METRIC_MAP_PATH", str(b))
    assert load_metric_map()[URI].broker_metric == "jitter_ms"
    monkeypatch.setenv("METRIC_MAP_PATH", str(a))
    assert load_metric_map()[URI].broker_metric == "latency_ms"


def test_a_missing_file_is_empty_and_a_created_one_is_read_next(tmp_path, monkeypatch):
    p = tmp_path / "later.json"
    monkeypatch.setenv("METRIC_MAP_PATH", str(p))
    assert load_metric_map() == {}
    assert load_metric_map() == {}
    assert metric_map_key()[1:] == (None,)
    p.write_text(json.dumps([_e()]))
    assert URI in load_metric_map()


def test_a_malformed_file_raises_every_time_and_is_read_every_time(tmp_path, monkeypatch, reads):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    monkeypatch.setenv("METRIC_MAP_PATH", str(p))
    for _ in range(2):
        with pytest.raises(ValueError):
            load_metric_map()
    assert reads.count(str(p)) == 2


def test_a_file_that_becomes_malformed_stops_answering_from_the_old_map(tmp_path, monkeypatch):
    p = _w(tmp_path, [_e()])
    monkeypatch.setenv("METRIC_MAP_PATH", str(p))
    assert URI in load_metric_map()
    p.write_text("[{broken")
    with pytest.raises(ValueError):
        load_metric_map()


def test_each_caller_gets_its_own_dict(tmp_path, monkeypatch):
    p = _w(tmp_path, [_e()])
    monkeypatch.setenv("METRIC_MAP_PATH", str(p))
    first = load_metric_map()
    first.clear()
    first["http://example.org/x"] = None
    again = load_metric_map()
    assert list(again) == [URI]
    assert again is not first


def test_metric_map_key_shape(tmp_path):
    p = _w(tmp_path, [_e()])
    key = metric_map_key(p)
    st = p.stat()
    assert key == (str(p.resolve()), st.st_ino, st.st_size, st.st_mtime_ns)
    assert metric_map_key(tmp_path / "nope.json") == (str((tmp_path / "nope.json").resolve()), None)


def test_clear_metric_map_cache_forces_a_read(tmp_path, monkeypatch, reads):
    p = _w(tmp_path, [_e()])
    monkeypatch.setenv("METRIC_MAP_PATH", str(p))
    load_metric_map()
    clear_metric_map_cache()
    load_metric_map()
    assert reads.count(str(p)) == 2


def test_an_explicit_path_argument_is_cached_too(tmp_path, reads):
    p = _w(tmp_path, [_e()])
    load_metric_map(p)
    load_metric_map(p)
    assert reads.count(str(p)) == 1


def test_a_five_metric_intent_reads_the_map_file_at_most_once(reads):
    from intent_management.tmf921_client import intent_bounds

    uris = [m.metric_uri for m in load_metric_map().values()]
    clear_metric_map_cache()
    del reads[:]
    ttl = ("@prefix quan: <http://tio.models.tmforum.org/tio/v3.6.0/QuantityOntology/> .\n"
           "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
           + "".join(f'_:c{i} quan:atLeast ( <{u}> [ rdf:value "{i + 1}"'
                     f'^^<http://www.w3.org/2001/XMLSchema#decimal> ] ) .\n'
                     for i, u in enumerate(uris)))
    out = intent_bounds({"expression": {"expressionValue": ttl}})
    assert len(out) == 5
    assert sum(1 for r in reads if r.endswith("metric_map.json")) <= 1
