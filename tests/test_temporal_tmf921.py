"""Temporal assess (as_of, mode) for source=tmf921: read-only, bounds read now."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import requests

from broker_engine import assess as assess_mod
from broker_engine.assess import assess_intent
from broker_engine.validation import ClientInputError, window_seconds
from data_contracts.envelope import build_observed
from data_contracts.intent_bindings import IntentBinding
from data_contracts.metric_map import MetricMapping
from intent_management import tmf921_source as src
from intent_management.tmf921_client import Tmf921Error
from intent_management.tmf921_source import assess_tmf921_as_of
from outcome_assurance.evaluate import Outcome

NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
LAT = "http://broadband-forum.org/Intent#LatencyMetric"
JIT = "http://broadband-forum.org/Intent#JitterMetric"
DOWN = "http://broadband-forum.org/Intent#DownstreamBandwidthMetric"
IID = "hsi-demo-1"
MMAP = {LAT: MetricMapping("latency_ms", LAT, "ms", "<="),
        JIT: MetricMapping("jitter_ms", JIT, "ms", "<="),
        DOWN: MetricMapping("downstream_bandwidth_mbps", DOWN, "Mbps", ">=")}
B_LAT = {IID: IntentBinding(IID, {LAT: ("olt1", "pon0/1")}, {LAT: 100.0})}
B_TWO = {IID: IntentBinding(IID, {LAT: ("olt1", "pon0/1"), JIT: ("olt1", "pon0/2")},
                            {LAT: 100.0, JIT: 10.0})}
B_DOWN = {IID: IntentBinding(IID, {DOWN: ("olt1", "pon0/1")}, {DOWN: 100.0})}
REQUIREMENT = "bounds read now from the TMF921 intent (no history is kept)"


def ttl(uri, val):
    return ('@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n'
            f'[] <http://x/p> ( <{uri}> [ rdf:value {val} ] ) .')


def intent(*pairs):
    return {"expression": {"expressionValue": "\n".join(ttl(u, v) for u, v in pairs)}}


class FakeClient:
    def __init__(self, doc=None, get_exc=None):
        self.doc = doc if doc is not None else intent((LAT, 100))
        self.get_exc = get_exc
        self.gets = self.lists = 0
        self.posts = []

    def get_intent(self, i):
        self.gets += 1
        if self.get_exc:
            raise self.get_exc
        return self.doc

    def list_reports(self, i):
        self.lists += 1
        return []

    def post_observation(self, *a, **kw):
        self.posts.append(a)


def make_join(values, age=10, stale=False):
    """values: {(broker_metric, aggregate): value}. Mimics the joiner: the
    reference instant is now - as_of and freshness is judged there."""
    calls = []

    def join(device, interface, metric, window, **kw):
        calls.append({"device": device, "interface": interface, "metric": metric,
                      "window": window, **kw})
        ref = kw["now"] - (timedelta(seconds=window_seconds(kw["as_of"], "as_of"))
                           if kw.get("as_of") else timedelta(0))
        v = values.get((metric, kw["aggregate"]))
        obs = None if v is None else ref - timedelta(seconds=10_000 if stale else age)
        return build_observed(f"{device}:{interface}", v, "victoriametrics", obs, ref)
    join.calls = calls
    return join


def run(client=None, join=None, bindings=B_LAT, mmap=MMAP, as_of="10m", mode=None,
        window="5m", iid=IID):
    return assess_tmf921_as_of(iid, window, as_of=as_of, mode=mode,
                               client=client or FakeClient(), join=join, now=NOW,
                               metric_map=mmap, bindings=bindings)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding", lambda i: None)
    src.TMF921_STORE._outcomes.clear()
    src._MARKS.clear()
    yield
    src.TMF921_STORE._outcomes.clear()
    src._MARKS.clear()


# ---------- behaviour ----------
def test_at_mode_uses_last_sample_at_instant():
    join = make_join({("latency_ms", "last"): 42.0, ("latency_ms", "max"): 500.0})
    out = run(join=join, as_of="10m")
    assert [c["aggregate"] for c in join.calls] == ["last"]
    assert join.calls[0]["as_of"] == "10m" and join.calls[0]["now"] == NOW
    assert join.calls[0]["device"] == "olt1" and join.calls[0]["interface"] == "pon0/1"
    assert out["verdict"] == "success"
    assert out["mode"] == "at" and out["as_of"] == "10m"
    assert out["evaluated_at"] == (NOW - timedelta(minutes=10)).isoformat()
    assert out["requirement"] == REQUIREMENT and out["source"] == "tmf921"
    assert out["per_interface"] == [{"condition": "LatencyMetric", "device": "olt1",
                                     "interface_name": "pon0/1", "outcome": "success",
                                     "observed": 42.0, "reason": None}]
    assert set(out) >= {"verdict", "per_interface", "reasons", "context", "dropped_count",
                        "intent_status", "last_outcome"}


def test_within_mode_finds_a_breach_in_the_window():
    join = make_join({("latency_ms", "last"): 42.0, ("latency_ms", "max"): 150.0,
                      ("latency_ms", "min"): 10.0})
    out = run(join=join, as_of="10m", mode="within")
    assert [c["aggregate"] for c in join.calls] == ["max"]
    assert out["verdict"] == "failure" and out["mode"] == "within"
    assert out["per_interface"][0]["observed"] == 150.0
    assert out["reasons"] == [
        "LatencyMetric: olt1/pon0/1: observed 150.0 does not satisfy <= 100.0"]


def test_within_without_as_of_ends_now():
    join = make_join({("latency_ms", "max"): 50.0})
    out = run(join=join, as_of=None, mode="within")
    assert join.calls[0]["as_of"] is None and out["verdict"] == "success"
    assert out["evaluated_at"] == NOW.isoformat() and out["as_of"] is None


def test_bandwidth_uses_lower_bound_worst_case():
    client = FakeClient(intent((DOWN, 100)))
    join = make_join({("downstream_bandwidth_mbps", "min"): 50.0,
                      ("downstream_bandwidth_mbps", "max"): 200.0,
                      ("downstream_bandwidth_mbps", "last"): 200.0})
    out = run(client, join, bindings=B_DOWN, mode="within")
    assert [c["aggregate"] for c in join.calls] == ["min"]
    assert out["verdict"] == "failure"
    assert out["per_interface"][0]["observed"] == 50.0
    assert out["per_interface"][0]["condition"] == "DownstreamBandwidthMetric"


def test_drifted_bound_is_inconclusive():
    join = make_join({("latency_ms", "last"): 42.0})
    out = run(FakeClient(intent((LAT, 200))), join)
    assert out["verdict"] == "inconclusive" and out["reasons"] == ["intent bound drifted"]
    assert out["intent_status"] == "unknown" and join.calls == []


def test_missing_operator_is_inconclusive_naming_the_metric():
    mmap = {LAT: MetricMapping("latency_ms", LAT, "ms")}  # no operator
    join = make_join({("latency_ms", "last"): 42.0})
    out = run(join=join, mmap=mmap)
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["no operator for metric: LatencyMetric"]
    assert join.calls == []


def test_unmapped_metric_is_inconclusive_naming_the_metric():
    out = run(join=make_join({}), mmap={})
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["unmapped metric: LatencyMetric"]


def test_one_failing_metric_fails_the_whole_intent():
    client = FakeClient(intent((LAT, 100), (JIT, 10)))
    join = make_join({("latency_ms", "last"): 42.0, ("jitter_ms", "last"): 25.0})
    out = run(client, join, bindings=B_TWO)
    assert out["verdict"] == "failure"
    by = {p["condition"]: p for p in out["per_interface"]}
    assert by["LatencyMetric"]["outcome"] == "success"
    assert by["JitterMetric"]["outcome"] == "failure"
    assert by["JitterMetric"]["interface_name"] == "pon0/2"
    assert out["reasons"] == ["JitterMetric: olt1/pon0/2: observed 25.0 does not satisfy <= 10.0"]


def test_failure_outranks_inconclusive_and_inconclusive_outranks_success():
    client = FakeClient(intent((LAT, 100), (JIT, 10)))
    missing = make_join({("jitter_ms", "last"): 25.0})  # latency missing, jitter fails
    assert run(client, missing, bindings=B_TWO)["verdict"] == "failure"
    ok_and_missing = make_join({("latency_ms", "last"): 42.0})
    out = run(client, ok_and_missing, bindings=B_TWO)
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["JitterMetric: olt1/pon0/2: missing observation"]


@pytest.mark.parametrize("mode", ["at", "within"])
def test_stale_evidence_is_never_success(mode):
    join = make_join({("latency_ms", "last"): 42.0, ("latency_ms", "max"): 42.0}, stale=True)
    out = run(join=join, mode=mode)
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["LatencyMetric: olt1/pon0/1: stale observation"]


def test_non_finite_value_is_never_success():
    join = make_join({("latency_ms", "last"): float("-inf")})
    out = run(join=join)
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == [
        "LatencyMetric: olt1/pon0/1: unresolved conflicts: invalid observation"]


# ---------- no side effects ----------
def test_no_observation_is_pushed():
    client = FakeClient()
    run(client, make_join({("latency_ms", "last"): 42.0}))
    run(client, make_join({("latency_ms", "max"): 420.0}), mode="within")
    assert client.posts == [] and client.lists == 0


def test_no_mark_is_written():
    run(join=make_join({("latency_ms", "last"): 42.0}))
    assert src._MARKS == {}


def test_fulfilment_not_updated():
    src.TMF921_STORE.fulfilment_update(IID, Outcome.FAILURE)
    with patch.object(src.TMF921_STORE, "fulfilment_update") as upd, \
            patch.object(src, "_record") as rec:
        out = run(join=make_join({("latency_ms", "last"): 42.0}))
        run(FakeClient(get_exc=Tmf921Error("down")), make_join({}))
        run(FakeClient(intent((LAT, 1))), make_join({}))
    upd.assert_not_called()
    rec.assert_not_called()
    assert out["verdict"] == "success" and out["last_outcome"] == "failure"
    assert src.TMF921_STORE.last_outcome(IID) is Outcome.FAILURE


# ---------- failures of collaborators ----------
def test_binding_store_down_is_inconclusive(monkeypatch):
    def down(_i):
        raise requests.ConnectionError("fuseki down: secret text")
    monkeypatch.setattr(src, "resolve_binding", down)
    monkeypatch.setattr(src, "load_bindings", lambda: {})
    out = assess_tmf921_as_of(IID, "5m", as_of="10m", mode=None, client=FakeClient(),
                              join=make_join({}), now=NOW, metric_map=MMAP)
    assert out["verdict"] == "inconclusive" and out["reasons"] == ["binding store unavailable"]
    assert "secret" not in str(out)


def test_server_down_is_inconclusive():
    out = run(FakeClient(get_exc=Tmf921Error("boom: server text")), make_join({}))
    assert out["verdict"] == "inconclusive" and out["reasons"] == ["intent server unavailable"]
    assert "server text" not in str(out)
    assert out["mode"] == "at" and out["source"] == "tmf921"


@pytest.mark.parametrize("kw", [{"as_of": "PT10M"}, {"as_of": "10"}, {"mode": "AT"},
                                {"mode": "sometime"}, {"as_of": "99999d"}, {"window": "5 m"},
                                {"iid": "../x"}])
def test_unsafe_input_is_a_client_error(kw):
    client, join = FakeClient(), make_join({})
    with pytest.raises(ClientInputError):
        run(client, join, **kw)
    assert client.gets == 0 and join.calls == []


# ---------- routing through assess_intent ----------
def test_assess_intent_routes_temporal_tmf921():
    join = make_join({})
    with patch.object(assess_mod, "assess_tmf921_as_of", return_value={"v": 1}) as t, \
            patch.object(assess_mod, "assess_via_tmf921") as push:
        assert assess_intent(IID, "5m", source="tmf921", as_of="10m", join=join) == {"v": 1}
        assess_intent(IID, "5m", source="tmf921", mode="within", join=join)
    push.assert_not_called()
    assert t.call_args_list[0].kwargs == {"as_of": "10m", "mode": None, "join": join}
    assert t.call_args_list[1].kwargs == {"as_of": None, "mode": "within", "join": join}


def test_assess_intent_tmf921_unsafe_as_of_is_client_error():
    with patch.object(assess_mod, "assess_tmf921_as_of") as t:
        with pytest.raises(ClientInputError):
            assess_intent(IID, "5m", source="tmf921", as_of="PT10M")
    t.assert_not_called()


def test_assess_intent_tmf921_without_temporal_arguments_takes_push_path():
    join = make_join({})
    with patch.object(assess_mod, "assess_tmf921_as_of") as t, \
            patch.object(assess_mod, "assess_via_tmf921", return_value={"p": 1}) as push:
        assert assess_intent(IID, "5m", source="tmf921", join=join) == {"p": 1}
    t.assert_not_called()
    push.assert_called_once_with(IID, "5m", join=join)
