from unittest.mock import Mock, patch

import pytest

from broker_engine.binding_resolver import resolve_binding
from broker_engine.validation import ClientInputError

URI = "http://broadband-forum.org/Intent#LatencyMetric"
URI2 = "http://broadband-forum.org/Intent#JitterMetric"
POST = "broker_engine.binding_resolver.requests.post"


def row(metric=URI, device="olt1", iface="pon0/1", pin="100.0"):
    return {"metric": {"value": metric}, "device": {"value": device},
            "iface": {"value": iface}, "pin": {"value": pin}}


def resp(rows):
    r = Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = {"results": {"bindings": rows}}
    return r


def test_returns_none_when_no_rows():
    with patch(POST, return_value=resp([])):
        assert resolve_binding("hsi-1") is None


def test_builds_sources_and_bounds_from_rows():
    rows = [row(), row(URI2, "olt1", "pon0/2", "10")]
    with patch(POST, return_value=resp(rows)) as p:
        b = resolve_binding("hsi-1")
    assert b.intent_id == "hsi-1"
    assert b.sources == {URI: ("olt1", "pon0/1"), URI2: ("olt1", "pon0/2")}
    assert b.bounds == {URI: 100.0, URI2: 10.0}
    assert '"hsi-1"' in p.call_args.kwargs["data"]["query"]


def test_identical_duplicate_rows_collapse():
    with patch(POST, return_value=resp([row(), row()])):
        assert resolve_binding("hsi-1").bounds == {URI: 100.0}


@pytest.mark.parametrize("other", [row(iface="pon0/2"), row(pin="5")])
def test_duplicate_metric_for_intent_is_invalid(other):
    with patch(POST, return_value=resp([row(), other])):
        with pytest.raises(ValueError):
            resolve_binding("hsi-1")


@pytest.mark.parametrize("pin", ["NaN", "INF", "-INF", "abc", ""])
def test_non_finite_pin_is_invalid(pin):
    with patch(POST, return_value=resp([row(pin=pin)])):
        with pytest.raises(ValueError):
            resolve_binding("hsi-1")


@pytest.mark.parametrize("bad", [dict(metric="not a uri"), dict(device="r1;drop"),
                                 dict(iface="a b")])
def test_bad_metric_uri_is_invalid(bad):
    with patch(POST, return_value=resp([row(**bad)])):
        with pytest.raises(ValueError):
            resolve_binding("hsi-1")


def test_malformed_result_is_invalid():
    r = Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = {"oops": 1}
    with patch(POST, return_value=r):
        with pytest.raises(ValueError):
            resolve_binding("hsi-1")


@pytest.mark.parametrize("iid", ['x" } ; DROP', "a b", "", "x\n", None])
def test_unsafe_intent_id_rejected_before_any_request(iid):
    with patch(POST) as p:
        with pytest.raises(ClientInputError):
            resolve_binding(iid)
    p.assert_not_called()
