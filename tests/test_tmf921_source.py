from datetime import datetime, timedelta, timezone

import pytest

from broker_engine import assess
from broker_engine.validation import ClientInputError
from data_contracts.envelope import build_observed
from data_contracts.intent_bindings import IntentBinding
from data_contracts.metric_map import MetricMapping
from intent_management.tmf921_client import Tmf921Error
from intent_management.tmf921_source import assess_via_tmf921

NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
URI = "http://broadband-forum.org/Intent#LatencyMetric"
URI2 = "http://broadband-forum.org/Intent#JitterMetric"
IID = "hsi-demo-1"
MMAP = {URI: MetricMapping("latency_ms", URI, "ms"),
        URI2: MetricMapping("jitter_ms", URI2, "ms")}
BIND = {IID: IntentBinding(IID, {URI: ("olt1", "pon0/1")}, {URI: 100.0})}
BIND2 = {IID: IntentBinding(IID, {URI: ("olt1", "pon0/1"), URI2: ("olt1", "pon0/2")},
                            {URI: 100.0, URI2: 10.0})}


def ttl(uri, val):
    return ('@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n'
            f'[] <http://x/p> ( <{uri}> [ rdf:value {val} ] ) .')


def intent(*pairs):
    return {"expression": {"expressionValue": "\n".join(ttl(u, v) for u, v in pairs)}}


def rep(rid, date, state="Fulfilled", reason=None):
    return {"id": rid, "creationDate": date, "intentHandlingState": state,
            "intentHandlingReason": reason, "expression": None}


OLD = rep("old", "2026-10-27T11:00:00Z", "Degraded",
          "Conditions not met: atLeast: bound violated: X: FAIL")
OLD_OK = rep("oldok", "2026-10-27T11:00:00Z")


class FakeClient:
    def __init__(self, intent_doc=None, pre=(), post=(), get_exc=None, push_exc=None, list_exc=None):
        self.intent_doc = intent_doc if intent_doc is not None else intent((URI, 100))
        self.reports = [list(pre)] + [list(x) for x in post]
        self.posts = []
        self.lists = 0
        self.get_exc, self.push_exc, self.list_exc = get_exc, push_exc, list_exc

    def get_intent(self, i):
        if self.get_exc:
            raise self.get_exc
        return self.intent_doc

    def list_reports(self, i):
        if self.list_exc:
            raise self.list_exc
        r = self.reports[min(self.lists, len(self.reports) - 1)]
        self.lists += 1
        return r

    def post_observation(self, i, uri, value, unit=None, obtained_at=None):
        if self.push_exc:
            raise self.push_exc
        self.posts.append((uri, value, unit, obtained_at))


def make_join(age=10, value=42.0, conflicts=False, missing=False):
    seen = []

    def join(device, interface, metric, window):
        seen.append((device, interface, metric, window))
        obs = None if missing else NOW - timedelta(seconds=age)
        env = build_observed(f"{device}:{interface}", None if missing else value,
                             "victoriametrics", obs, NOW)
        if conflicts:
            env.quality.conflicts.append("multiple series match selector (2)")
        return env
    join.seen = seen
    return join


def run(client, join=None, bindings=BIND, mmap=MMAP, iid=IID, window="5m", **kw):
    sleeps = []
    out = assess_via_tmf921(iid, window, client=client, join=join or make_join(),
                            now=NOW, metric_map=mmap, bindings=bindings,
                            sleep=sleeps.append, **kw)
    out["_sleeps"] = sleeps
    return out


@pytest.fixture(autouse=True)
def clean_store():
    assess.DEFAULT_STORE._outcomes.clear()
    yield
    assess.DEFAULT_STORE._outcomes.clear()


def test_success_pushes_observed_at_as_obtained_at():
    join = make_join(age=10)
    c = FakeClient(pre=[OLD], post=[[rep("new", "2026-10-27T12:00:01Z")]])
    out = run(c, join)
    assert out["verdict"] == "success"
    assert out["source"] == "tmf921"
    assert set(out) >= {"verdict", "per_interface", "reasons", "context", "dropped_count",
                        "intent_status", "last_outcome", "source"}
    assert c.posts == [(URI, 42.0, "ms", NOW - timedelta(seconds=10))]
    assert join.seen == [("olt1", "pon0/1", "latency_ms", "5m")]
    assert out["context"][0]["entity_id"] == "olt1:pon0/1"


def test_violation_is_failure():
    c = FakeClient(post=[[rep("n", "2026-10-27T12:00:01Z", "Degraded",
                              "Conditions not met: atLeast: LatencyMetric: FAIL")]])
    assert run(c)["verdict"] == "failure"


def test_stale_envelope_not_pushed_and_old_fulfilled_ignored():
    c = FakeClient(pre=[OLD_OK])
    out = run(c, make_join(age=1000))
    assert c.posts == [] and c.lists == 0
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["stale observation: LatencyMetric"]


def test_missing_envelope_not_pushed():
    c = FakeClient(pre=[OLD_OK])
    out = run(c, make_join(missing=True))
    assert c.posts == [] and out["verdict"] == "inconclusive"
    assert out["reasons"] == ["missing observation: LatencyMetric"]


def test_conflicted_envelope_not_pushed():
    c = FakeClient(pre=[OLD_OK])
    out = run(c, make_join(conflicts=True))
    assert c.posts == [] and out["verdict"] == "inconclusive"
    assert out["reasons"] == ["conflicting observation: LatencyMetric"]


def test_unmapped_uri():
    out = run(FakeClient(), mmap={})
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["unmapped metric: LatencyMetric"]


def test_no_binding():
    c = FakeClient()
    out = run(c, bindings={})
    assert out["verdict"] == "inconclusive" and out["reasons"] == ["no binding for intent"]
    assert c.posts == []


def test_invalid_binding_load_error(monkeypatch):
    def boom(*a, **k):
        raise ValueError("duplicate key")
    monkeypatch.setattr("intent_management.tmf921_source.load_bindings", boom)
    out = assess_via_tmf921(IID, "5m", client=FakeClient(), join=make_join(),
                            metric_map=MMAP, sleep=lambda s: None)
    assert out["verdict"] == "inconclusive" and out["reasons"] == ["invalid binding"]


def test_poll_timeout_only_prepush_reports():
    c = FakeClient(pre=[OLD_OK], post=[[OLD_OK]])
    out = run(c, poll_interval_s=1.0, poll_timeout_s=3.0)
    assert out["verdict"] == "inconclusive" and out["reasons"] == ["no new report"]
    assert len(c.posts) == 1
    assert out["_sleeps"] == [1.0, 1.0, 1.0]


def test_old_report_first_then_new():
    new = rep("new", "2026-10-27T12:00:02Z")
    c = FakeClient(pre=[OLD], post=[[OLD], [OLD], [new, OLD]])
    out = run(c)
    assert out["verdict"] == "success"
    assert len(out["_sleeps"]) == 2


def test_tmf921_error_on_push():
    c = FakeClient(push_exc=Tmf921Error("x"))
    out = run(c)
    assert out["verdict"] == "inconclusive" and out["reasons"] == ["intent server unavailable"]


def test_unexpected_exception_fails_closed():
    c = FakeClient(list_exc=RuntimeError("secret server text"))
    out = run(c)
    assert out["verdict"] == "inconclusive"
    assert "secret" not in str(out)


def test_tmf921_error_on_get_intent():
    out = run(FakeClient(get_exc=Tmf921Error("x")))
    assert out["reasons"] == ["intent server unavailable"]


def test_bound_drifted_zero_pushes():
    c = FakeClient(intent_doc=intent((URI, 50)), post=[[rep("n", "2026-10-27T12:00:01Z")]])
    out = run(c)
    assert out["verdict"] == "inconclusive" and out["reasons"] == ["intent bound drifted"]
    assert c.posts == [] and c.lists == 0


def test_unreadable_bound_zero_pushes():
    c = FakeClient(intent_doc={"expression": None})
    out = run(c)
    assert out["reasons"] == ["intent bound drifted"] and c.posts == []


def test_last_outcome_recorded():
    c = FakeClient(post=[[rep("n", "2026-10-27T12:00:01Z")]])
    first = run(c)
    assert first["last_outcome"] is None
    assert assess.DEFAULT_STORE.last_outcome(IID).value == "success"
    c2 = FakeClient(intent_doc=intent((URI, 50)))
    assert run(c2)["last_outcome"] == "success"
    assert assess.DEFAULT_STORE.last_outcome(IID).value == "inconclusive"


@pytest.mark.parametrize("window", ["5x", "5m\n", "", "abc"])
def test_bad_window_before_http(window):
    c = FakeClient()
    with pytest.raises(ClientInputError):
        run(c, window=window)
    assert c.lists == 0 and c.posts == []


@pytest.mark.parametrize("iid", ["abc\n", "a/b", "", "x" * 65])
def test_bad_intent_id_before_http(iid):
    c = FakeClient()
    with pytest.raises(ClientInputError):
        run(c, iid=iid)
    assert c.posts == []


def test_two_metrics_all_pushable_success():
    c = FakeClient(intent_doc=intent((URI, 100), (URI2, 10)),
                   post=[[rep("n", "2026-10-27T12:00:01Z")]])
    out = run(c, bindings=BIND2)
    assert out["verdict"] == "success" and len(c.posts) == 2


def test_two_metrics_one_stale_never_success():
    def join(device, interface, metric, window):
        age = 1000 if metric == "jitter_ms" else 5
        return build_observed(f"{device}:{interface}", 1.0, "vm", NOW - timedelta(seconds=age), NOW)
    c = FakeClient(intent_doc=intent((URI, 100), (URI2, 10)),
                   post=[[rep("n", "2026-10-27T12:00:01Z")]])
    out = run(c, join, bindings=BIND2)
    assert out["verdict"] == "inconclusive" and len(c.posts) == 1
    assert "stale observation: JitterMetric" in out["reasons"]


def test_two_metrics_failure_wins():
    c = FakeClient(intent_doc=intent((URI, 100), (URI2, 10)),
                   post=[[rep("n", "2026-10-27T12:00:01Z", "Degraded",
                              "Conditions not met: atLeast: JitterMetric: FAIL")]])
    assert run(c, bindings=BIND2)["verdict"] == "failure"
