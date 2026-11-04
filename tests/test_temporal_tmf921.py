"""Temporal assess (as_of, known_at, mode) for source=tmf921: read-only.

A past question reads the goal and placement in force then from the graph fluents and
makes no TMF921 call; a question about now also checks the live goal and lifecycle."""
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
MMAP = {LAT: MetricMapping("latency_ms", LAT, "ms", "<"),
        JIT: MetricMapping("jitter_ms", JIT, "ms", "<"),
        DOWN: MetricMapping("downstream_bandwidth_mbps", DOWN, "Mbps", ">=")}
B_LAT = {IID: IntentBinding(IID, {LAT: ("olt1", "pon0/1")}, {LAT: 100.0})}
B_TWO = {IID: IntentBinding(IID, {LAT: ("olt1", "pon0/1"), JIT: ("olt1", "pon0/2")},
                            {LAT: 100.0, JIT: 10.0})}
B_DOWN = {IID: IntentBinding(IID, {DOWN: ("olt1", "pon0/1")}, {DOWN: 100.0})}
REQUIREMENT = ("goal in force now, pinned in the network graph and checked against the "
               "TMF921 intent")
HISTORICAL = "goal in force at the requested time, as recorded in the network graph"


def ttl(uri, val):
    return ('@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n'
            f'[] <http://x/p> ( <{uri}> [ rdf:value {val} ] ) .')


def intent(*pairs):
    return {"lifecycleStatus": "ACTIVE",
            "expression": {"expressionValue": "\n".join(ttl(u, v) for u, v in pairs)}}


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
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding", lambda i, **kw: None)
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
    assert out["requirement"] == HISTORICAL and out["source"] == "tmf921"
    assert out["intent_status"] == "unknown" and out["known_at"] == NOW.isoformat()
    assert out["goals"] == [{"metric": "LatencyMetric", "value": 100.0,
                             "valid_from": None, "valid_to": None}]
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
        "LatencyMetric: olt1/pon0/1: observed 150.0 does not satisfy < 100.0"]


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
    client = FakeClient(intent((LAT, 200)))
    out = run(client, join, as_of=None, mode="within")
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["goal changed: LatencyMetric pinned 100, now 200"]
    assert out["goal_changes"] == [{"metric": "LatencyMetric", "pinned": 100.0,
                                     "current": 200.0}]
    assert out["mode"] == "within" and out["as_of"] is None
    assert out["requirement"] == REQUIREMENT and "goals" not in out and "known_at" not in out
    assert out["intent_status"] == "unknown" and join.calls == [] and client.posts == []

    # The same documents for a past question: no TMF921 call, judged against the pinned goal.
    past_client = FakeClient(intent((LAT, 200)))
    past = run(past_client, make_join({("latency_ms", "last"): 42.0}))
    assert past["verdict"] == "success" and past_client.gets == 0


def test_unreadable_bound_keeps_old_reason():
    doc = {"lifecycleStatus": "ACTIVE", "expression": None}
    out = run(FakeClient(doc), as_of=None, mode="within")
    assert out["reasons"] == ["intent goal unreadable"] and "goal_changes" not in out
    past_client = FakeClient(doc)
    run(past_client, make_join({}))
    assert past_client.gets == 0


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
    assert out["reasons"] == ["JitterMetric: olt1/pon0/2: observed 25.0 does not satisfy < 10.0"]


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
    assert out["reasons"] == ["LatencyMetric: olt1/pon0/1: missing observation"]


# ---------- no side effects ----------
def test_no_observation_is_pushed():
    client = FakeClient()
    run(client, make_join({("latency_ms", "last"): 42.0}))
    run(client, make_join({("latency_ms", "max"): 420.0}), mode="within")
    assert client.posts == [] and client.lists == 0 and client.gets == 0


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
    def down(_i, **_kw):
        raise requests.ConnectionError("fuseki down: secret text")
    monkeypatch.setattr(src, "resolve_binding", down)
    for kw in ({"as_of": "10m"}, {"as_of": None, "known_at": "1h"}, {"as_of": None}):
        out = assess_tmf921_as_of(IID, "5m", mode=None, client=FakeClient(),
                                  join=make_join({}), now=NOW, metric_map=MMAP, **kw)
        assert out["verdict"] == "inconclusive"
        assert out["reasons"] == ["binding store unavailable"]
        assert "secret" not in str(out)


def test_server_down_is_inconclusive():
    out = run(FakeClient(get_exc=Tmf921Error("boom: server text")), make_join({}),
              as_of=None, mode="within")
    assert out["verdict"] == "inconclusive" and out["reasons"] == ["intent server unavailable"]
    assert "server text" not in str(out)
    assert out["mode"] == "within" and out["source"] == "tmf921"
    # A past question never reads the server, so its being down changes nothing.
    past = run(FakeClient(get_exc=Tmf921Error("boom")), make_join({("latency_ms", "last"): 1.0}))
    assert past["verdict"] == "success"


@pytest.mark.parametrize("kw", [{"as_of": "PT10M"}, {"as_of": "10"}, {"mode": "AT"},
                                {"known_at": "PT1H"}, {"known_at": "99999d"},
                                {"mode": "sometime"}, {"as_of": "99999d"}, {"window": "5 m"},
                                {"iid": "../x"}])
def test_unsafe_input_is_a_client_error(kw):
    client, join = FakeClient(), make_join({})
    known = kw.pop("known_at", None)
    with pytest.raises(ClientInputError):
        assess_tmf921_as_of(kw.get("iid", IID), kw.get("window", "5m"),
                            as_of=kw.get("as_of", "10m"), mode=kw.get("mode"),
                            known_at=known, client=client, join=join, now=NOW,
                            metric_map=MMAP, bindings=B_LAT)
    assert client.gets == 0 and join.calls == []


# ---------- routing through assess_intent ----------
def test_assess_intent_routes_temporal_tmf921():
    join = make_join({})
    with patch.object(assess_mod, "assess_tmf921_as_of", return_value={"v": 1}) as t, \
            patch.object(assess_mod, "assess_via_tmf921") as push:
        assert assess_intent(IID, "5m", source="tmf921", as_of="10m", join=join) == {"v": 1}
        assess_intent(IID, "5m", source="tmf921", mode="within", join=join)
    push.assert_not_called()
    assert t.call_args_list[0].kwargs == {"as_of": "10m", "mode": None, "known_at": None,
                                          "join": join}
    assert t.call_args_list[1].kwargs == {"as_of": None, "mode": "within", "known_at": None,
                                          "join": join}


def test_assess_intent_routes_known_at_alone_to_the_temporal_path():
    join = make_join({})
    with patch.object(assess_mod, "assess_tmf921_as_of", return_value={"v": 1}) as t, \
            patch.object(assess_mod, "assess_via_tmf921") as push:
        assess_intent(IID, "5m", known_at="1h", join=join)
        assess_intent(IID, "5m", as_of="2d", known_at="36h", mode="within", join=join)
    push.assert_not_called()
    assert t.call_args_list[0].kwargs == {"as_of": None, "mode": None, "known_at": "1h",
                                          "join": join}
    assert t.call_args_list[1].kwargs == {"as_of": "2d", "mode": "within", "known_at": "36h",
                                          "join": join}


@pytest.mark.parametrize("bad", ["PT1H", "1", "31d", "720h1", 5])
def test_assess_intent_validates_known_at_before_any_io(bad):
    with patch.object(assess_mod, "assess_tmf921_as_of") as t, \
            patch.object(assess_mod, "assess_via_tmf921") as push:
        with pytest.raises(ClientInputError):
            assess_intent(IID, "5m", known_at=bad)
    t.assert_not_called()
    push.assert_not_called()


def test_known_at_over_the_retention_names_no_caller_text():
    with pytest.raises(ClientInputError) as err:
        assess_intent(IID, "5m", known_at="31d")
    assert str(err.value) == "known_at is further back than the retention period"


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


# ---------- lifecycle ----------
def _with_status(**kw):
    d = intent((LAT, 100))
    d.pop("lifecycleStatus")
    d.update(kw)
    return d


@pytest.mark.parametrize("doc,status", [
    (_with_status(lifecycleStatus="TERMINATED"), "TERMINATED"),
    (_with_status(lifecycleStatus="ACKNOWLEDGED"), "ACKNOWLEDGED"),
    (_with_status(lifecycleStatus="SUSPENDED"), "SUSPENDED"),
    (_with_status(), "unknown"),
    (_with_status(lifecycleStatus="evil <script> http://leak"), "unknown"),
    (_with_status(lifecycleStatus=None), "unknown"),
])
def test_non_active_intent_is_inconclusive_before_telemetry(doc, status):
    join = make_join({("latency_ms", "last"): 1.0})
    client = FakeClient(doc=doc)
    out = run(client=client, join=join, as_of=None, mode="at")
    assert out["verdict"] == "inconclusive" and out["reasons"] == ["intent not active"]
    assert out["intent_status"] == status
    assert join.calls == []
    assert client.posts == [] and client.lists == 0 and not src._MARKS
    assert "leak" not in str(out) and "script" not in str(out)


def test_active_intent_still_assessed():
    out = run(join=make_join({("latency_ms", "last"): 1.0}), as_of=None, mode="at")
    assert out["verdict"] == "success" and out["intent_status"] == "active"


@pytest.mark.parametrize("status", ["TERMINATED", "ACKNOWLEDGED", "SUSPENDED", None])
def test_a_past_question_is_answered_whatever_the_intent_lifecycle(status):
    doc = _with_status(lifecycleStatus=status)
    client = FakeClient(doc=doc)
    out = run(client=client, join=make_join({("latency_ms", "last"): 1.0}))
    assert out["verdict"] == "success" and out["intent_status"] == "unknown"
    assert client.gets == 0 and client.lists == 0 and client.posts == []


def test_conflicting_series_behind_one_bound_is_inconclusive():
    def join(device, interface, metric, window, **kw):
        ref = kw["now"] - timedelta(minutes=10)
        env = build_observed(f"{device}:{interface}", 1.0, "victoriametrics",
                             ref - timedelta(seconds=10), ref)
        env.quality.conflicts.append("multiple series")
        return env
    for mode in (None, "within"):
        out = run(join=join, mode=mode)
        assert out["verdict"] == "inconclusive"


# ---------- history: the goal in force then (TEMP-03) ----------
from broker_engine.binding_resolver import resolve_binding as real_resolve  # noqa: E402
from data_contracts import fluents as fl  # noqa: E402
from tests.fluent_helpers import IFACES_TTL, NS, GraphFuseki  # noqa: E402

import rdflib  # noqa: E402

DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
BIND = f"{NS}binding-hsi-demo-1-LatencyMetric"
IF0 = f"{NS}if-olt1-pon0"
IF1 = f"{NS}if-olt1-pon1"
GRAPH_POST = "broker_engine.binding_resolver.requests.post"
DEFAULT_QUERY = "http://fuseki.test/ds/query"


def graph_store(goal_changes=True):
    """A GraphFuseki holding goal 100 [NOW-3d, NOW-1d), goal 150 from NOW-1d, and a
    placement olt1/pon0 from NOW-3d."""
    fx = GraphFuseki()
    fx.graph.parse(data=IFACES_TTL, format="turtle")
    g = rdflib.Graph()
    fl.add_binding(g, BIND, IID, LAT)
    fl.add_goal(g, BIND, 100.0, NOW - 3 * DAY, NOW - 3 * DAY, NOW - DAY if goal_changes else None)
    if goal_changes:
        fl.add_goal(g, BIND, 150.0, NOW - DAY, NOW - DAY)
    fl.add_placement(g, BIND, IF0, NOW - 3 * DAY, NOW - 3 * DAY)
    fx.post("http://fuseki.test/ds/update", data=fl.guarded_insert(g, []).encode())
    return fx


@pytest.fixture
def real_resolver(monkeypatch):
    """The real resolver over an in-memory graph; only requests.post is replaced."""
    holder = {}

    def resolve(intent_id, *a, **kw):
        with patch(GRAPH_POST, side_effect=holder["fx"].post):
            return real_resolve(intent_id, DEFAULT_QUERY, **kw)
    monkeypatch.setattr(src, "resolve_binding", resolve)
    return holder


def graph_run(client=None, join=None, window="5m", **kw):
    return assess_tmf921_as_of(IID, window, mode=kw.pop("mode", None),
                               as_of=kw.pop("as_of", None), known_at=kw.pop("known_at", None),
                               client=client or FakeClient(), join=join, now=NOW,
                               metric_map=MMAP, **kw)


def assert_untouched(client):
    assert client.gets == 0 and client.lists == 0 and client.posts == []
    assert src._MARKS == {}
    assert src.TMF921_STORE.last_outcome(IID) is None


def test_as_of_before_a_replacement_is_judged_against_the_goal_then(real_resolver):
    real_resolver["fx"] = graph_store()
    client = FakeClient(intent((LAT, 999)))  # the live goal is neither 100 nor 150
    join = make_join({("latency_ms", "last"): 120.0})

    old = graph_run(client, join, as_of="2d")
    assert old["verdict"] == "failure"  # 120 is not under 100
    assert old["goals"] == [{"metric": "LatencyMetric", "value": 100.0,
                             "valid_from": (NOW - 3 * DAY).isoformat(),
                             "valid_to": (NOW - DAY).isoformat()}]
    assert old["known_at"] == NOW.isoformat()
    assert old["requirement"] == HISTORICAL and old["intent_status"] == "unknown"
    assert old["evaluated_at"] == (NOW - 2 * DAY).isoformat()
    assert old["reasons"] == ["LatencyMetric: olt1/pon0: observed 120.0 does not satisfy < 100.0"]

    new = graph_run(client, join, as_of="12h")
    assert new["verdict"] == "success"  # 120 is under 150
    assert new["goals"] == [{"metric": "LatencyMetric", "value": 150.0,
                             "valid_from": (NOW - DAY).isoformat(), "valid_to": None}]
    assert_untouched(client)


def test_known_at_alone_is_a_past_question(real_resolver):
    real_resolver["fx"] = graph_store()
    client = FakeClient()
    join = make_join({("latency_ms", "last"): 120.0})
    out = graph_run(client, join, known_at="1h")
    assert out["verdict"] == "success" and out["as_of"] is None
    assert out["evaluated_at"] == NOW.isoformat()
    assert out["known_at"] == (NOW - HOUR).isoformat()
    assert out["requirement"] == HISTORICAL and out["intent_status"] == "unknown"
    assert join.calls[0]["as_of"] is None
    assert_untouched(client)


def test_a_time_no_fluent_covers_is_inconclusive_and_asks_nothing(real_resolver):
    real_resolver["fx"] = graph_store()
    client = FakeClient()
    join = make_join({("latency_ms", "last"): 1.0})
    out = graph_run(client, join, as_of="4d")  # before the first fluent: before migration 002
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["no goal in force: LatencyMetric",
                              "no placement in force: LatencyMetric"]
    assert join.calls == [] and out["goals"] == []
    assert_untouched(client)


def test_a_current_question_still_checks_the_live_goal(real_resolver):
    real_resolver["fx"] = graph_store()
    client = FakeClient(intent((LAT, 150)))
    out = graph_run(client, make_join({("latency_ms", "last"): 120.0}), mode="at")
    assert out["verdict"] == "success" and out["intent_status"] == "active"
    assert out["requirement"] == REQUIREMENT and client.gets == 1
    drifted = graph_run(FakeClient(intent((LAT, 100))), make_join({}), mode="at")
    assert drifted["reasons"] == ["goal changed: LatencyMetric pinned 150, now 100"]


# ---------- TC4 and the within window (TEMP-04, D-20) ----------
def corrected_store():
    """Placement olt1/pon0 from NOW-3d, corrected at NOW-1d to olt1/pon1 with the same
    valid interval: the old record is superseded, the new one is recorded at NOW-1d."""
    fx = graph_store(goal_changes=False)
    old = fl.fluent_iri(BIND, "place", NOW - 3 * DAY)
    g = rdflib.Graph()
    fl.add_placement(g, BIND, IF1, NOW - 3 * DAY, NOW - DAY, kind="place-reissue")
    fl.supersede(g, old, NOW - DAY)
    fx.post("http://fuseki.test/ds/update",
            data=fl.guarded_insert(g, [old], closed_by=("supersededAt",)).encode())
    return fx


def test_tc4_known_at_before_and_after_a_correction_gives_different_placements(real_resolver):
    real_resolver["fx"] = corrected_store()
    client = FakeClient()
    before_join = make_join({("latency_ms", "last"): 50.0})
    before = graph_run(client, before_join, as_of="2d", known_at="36h")
    after_join = make_join({("latency_ms", "last"): 50.0})
    after = graph_run(client, after_join, as_of="2d", known_at="12h")

    assert before["verdict"] == after["verdict"] == "success"
    assert [(c["device"], c["interface"]) for c in before_join.calls] == [("olt1", "pon0")]
    assert [(c["device"], c["interface"]) for c in after_join.calls] == [("olt1", "pon1")]
    assert before["per_interface"][0]["interface_name"] == "pon0"
    assert after["per_interface"][0]["interface_name"] == "pon1"
    assert before["known_at"] == (NOW - 36 * HOUR).isoformat()
    assert after["known_at"] == (NOW - 12 * HOUR).isoformat()
    assert_untouched(client)


def test_a_time_before_the_first_fluent_is_inconclusive_not_success(real_resolver):
    real_resolver["fx"] = graph_store()
    client = FakeClient()
    join = make_join({("latency_ms", "last"): 1.0, ("latency_ms", "max"): 1.0})
    for mode in (None, "within"):
        out = graph_run(client, join, as_of="4d", mode=mode)
        assert out["verdict"] == "inconclusive"
        assert out["reasons"] == ["no goal in force: LatencyMetric",
                                  "no placement in force: LatencyMetric"]
    assert join.calls == []
    assert_untouched(client)


def test_window_a_goal_that_started_inside_a_within_window_is_inconclusive(real_resolver):
    real_resolver["fx"] = graph_store()  # goal 150 started at NOW - 24h
    client = FakeClient()
    join = make_join({("latency_ms", "max"): 120.0, ("latency_ms", "last"): 120.0})
    # Window [NOW-25h, NOW-23h] contains the change at NOW-24h.
    inside = graph_run(client, join, as_of="23h", mode="within", window="2h")
    assert inside["verdict"] == "inconclusive"
    assert inside["reasons"] == ["goal not in force for the whole window: LatencyMetric"]
    assert inside["per_interface"][0]["reason"] == inside["reasons"][0]
    assert join.calls == []
    # Mode "at" is not affected, and a window that starts after the change is judged.
    assert graph_run(client, join, as_of="23h", window="2h")["verdict"] == "success"
    later = graph_run(client, join, as_of="12h", mode="within", window="2h")
    assert later["verdict"] == "success" and later["goals"][0]["value"] == 150.0
    # The goal that was in force for the whole window is judged as before.
    whole = graph_run(client, join, as_of="40h", mode="within", window="2h")
    assert whole["verdict"] == "failure" and whole["goals"][0]["value"] == 100.0
    assert_untouched(client)


def test_window_a_placement_that_started_inside_a_within_window_is_inconclusive(real_resolver):
    real_resolver["fx"] = corrected_store()  # the corrected record starts at NOW - 3d
    fx = real_resolver["fx"]
    g = rdflib.Graph()
    old = fl.fluent_iri(BIND, "place-reissue", NOW - DAY)
    fl.close(g, old, NOW - 12 * HOUR)
    fl.add_placement(g, BIND, IF0, NOW - 12 * HOUR, NOW - 12 * HOUR)
    fx.post("http://fuseki.test/ds/update", data=fl.guarded_insert(g, [old]).encode())
    join = make_join({("latency_ms", "max"): 50.0})
    out = graph_run(FakeClient(), join, as_of="11h", mode="within", window="2h")
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["placement not in force for the whole window: LatencyMetric"]
    assert join.calls == []


def test_window_applies_to_a_within_question_about_now(real_resolver):
    real_resolver["fx"] = graph_store()
    client = FakeClient(intent((LAT, 150)))
    join = make_join({("latency_ms", "max"): 120.0})
    # Goal 150 started 24h ago, so a 2d window about now is not covered by one goal.
    out = graph_run(client, join, mode="within", window="2d")
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["goal not in force for the whole window: LatencyMetric"]
    assert join.calls == []
    assert graph_run(client, join, mode="within", window="5m")["verdict"] == "success"


def test_window_an_injected_binding_without_intervals_is_not_checked():
    join = make_join({("latency_ms", "max"): 50.0})
    assert run(join=join, mode="within", window="2h")["verdict"] == "success"
