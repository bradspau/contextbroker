import json
import pytest
from data_contracts.intent_bindings import load_bindings

URI = "http://broadband-forum.org/Intent#LatencyMetric"


def _w(tmp_path, data):
    p = tmp_path / "b.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data))
    return p


def _spec(**kw):
    d = {"device": "olt1", "interface": "pon0/1", "bound": 25.0}
    d.update(kw)
    return d


def test_load_ok(tmp_path):
    b = load_bindings(_w(tmp_path, {"i1": {URI: _spec(bound=25)}}))
    assert b["i1"].sources[URI] == ("olt1", "pon0/1")
    assert b["i1"].bounds[URI] == 25.0


def test_shipped_file_is_empty():
    assert load_bindings() == {}


def test_binding_two_sources_for_one_metric_rejected(tmp_path):
    raw = ('{"i1": {"%s": {"device":"a","interface":"b","bound":1},'
           ' "%s": {"device":"c","interface":"d","bound":1}}}' % (URI, URI))
    with pytest.raises(ValueError):
        load_bindings(_w(tmp_path, raw))


def test_binding_unsafe_device_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_bindings(_w(tmp_path, {"i1": {URI: _spec(device="a;b")}}))


def test_load_bindings_missing_file_gives_empty(tmp_path):
    assert load_bindings(tmp_path / "nope.json") == {}


def test_binding_missing_bound_rejected(tmp_path):
    s = _spec()
    del s["bound"]
    with pytest.raises(ValueError):
        load_bindings(_w(tmp_path, {"i1": {URI: s}}))


@pytest.mark.parametrize("bad", [True, "25", None, float("nan"), float("inf")])
def test_binding_nonfinite_bound_rejected(tmp_path, bad):
    raw = json.dumps({"i1": {URI: _spec(bound=bad)}})  # emits NaN/Infinity literals
    with pytest.raises(ValueError):
        load_bindings(_w(tmp_path, raw))


def test_malformed_shape_rejected(tmp_path):
    for data in ([], {"i1": []}, {"i1": {URI: "x"}}):
        with pytest.raises(ValueError):
            load_bindings(_w(tmp_path, data))


@pytest.mark.parametrize("kw", [{"device": "olt1\n"}, {"interface": "pon0/1\n"}])
def test_trailing_newline_device_interface_rejected(tmp_path, kw):
    with pytest.raises(ValueError):
        load_bindings(_w(tmp_path, {"i1": {URI: _spec(**kw)}}))


def test_trailing_newline_intent_and_uri_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_bindings(_w(tmp_path, {"i1\n": {URI: _spec()}}))
    with pytest.raises(ValueError):
        load_bindings(_w(tmp_path, {"i1": {URI + "\n": _spec()}}))


def test_unsafe_intent_id_and_uri_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_bindings(_w(tmp_path, {"i 1": {URI: _spec()}}))
    with pytest.raises(ValueError):
        load_bindings(_w(tmp_path, {"i1": {"ftp://x/y": _spec()}}))


def test_duplicate_intent_key_rejected(tmp_path):
    raw = '{"i1": {}, "i1": {}}'
    with pytest.raises(ValueError):
        load_bindings(_w(tmp_path, raw))


def test_huge_int_bound_rejected_as_valueerror(tmp_path):
    raw = '{"i1": {"%s": {"device":"a","interface":"b","bound":1%s}}}' % (URI, "0" * 400)
    with pytest.raises(ValueError):
        load_bindings(_w(tmp_path, raw))
