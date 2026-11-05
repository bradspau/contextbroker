from datetime import datetime, timedelta, timezone

import pytest
import requests

import threading

from intent_management import tmf921_source as src
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
    def __init__(self, intent_doc=None, docs=None, pre=(), post=(), get_exc=None, push_exc=None, list_exc=None):
        self.intent_doc = intent_doc if intent_doc is not None else intent((URI, 100))
        self.reports = [list(pre)] + [list(x) for x in post]
        self.docs = list(docs) if docs else None
        self.gets = 0
        self.posts = []
        self.lists = 0
        self.get_exc, self.push_exc, self.list_exc = get_exc, push_exc, list_exc

    def get_intent(self, i):
        self.gets += 1
        if self.get_exc:
            raise self.get_exc
        if self.docs:
            return self.docs[min(self.gets, len(self.docs)) - 1]
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

    def join(device, interface, metric, window, **kw):
        seen.append((device, interface, metric, window))
        join.kw = kw
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
                            sleep=sleeps.append,
                            **{"monotonic": lambda: 0.0, **kw})
    out["_sleeps"] = sleeps
    return out


@pytest.fixture(autouse=True)
def no_live_graph(monkeypatch):
    # No test here may reach Fuseki by accident; tests needing a graph result override this.
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding", lambda i: None)


@pytest.fixture(autouse=True)
def clean_store():
    from broker_state.process import reset_store

    reset_store()
    yield
    reset_store()


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
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding", boom)
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
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["goal changed: LatencyMetric pinned 100, now 50"]
    assert c.posts == [] and c.lists == 0


def test_bound_change_names_metric_and_both_values():
    c = FakeClient(intent_doc=intent((URI, 50)))
    out = run(c)
    assert out["verdict"] == "inconclusive" and out["intent_status"] == "unknown"
    assert out["reasons"] == ["goal changed: LatencyMetric pinned 100, now 50"]
    assert out["goal_changes"] == [{"metric": "LatencyMetric", "pinned": 100.0,
                                     "current": 50.0}]
    assert c.posts == []


def test_bound_change_names_only_the_changed_metric():
    c = FakeClient(intent_doc=intent((URI, 100), (URI2, 7)))
    out = run(c, bindings=BIND2)
    assert out["reasons"] == ["goal changed: JitterMetric pinned 10, now 7"]
    assert out["goal_changes"] == [{"metric": "JitterMetric", "pinned": 10.0,
                                     "current": 7.0}]
    assert c.posts == []


def test_bound_set_change_names_no_server_metric():
    evil = "http://example.org/evil/SECRET"
    c = FakeClient(intent_doc=intent((URI, 100), (evil, 5)))
    out = run(c)
    assert out["reasons"] == ["goal set changed"]
    assert "goal_changes" not in out
    assert "SECRET" not in str(out) and c.posts == []


def test_bound_set_and_value_change_both_reported():
    evil = "http://example.org/evil/SECRET"
    c = FakeClient(intent_doc=intent((URI, 50), (evil, 5)))
    out = run(c)
    assert out["reasons"] == ["goal changed: LatencyMetric pinned 100, now 50",
                              "goal set changed"]
    assert "SECRET" not in str(out)


def test_number_formatting():
    bind = {IID: IntentBinding(IID, {URI: ("olt1", "pon0/1")}, {URI: 25.0})}
    out = run(FakeClient(intent_doc=intent((URI, 40.535))), bindings=bind)
    assert out["reasons"] == ["goal changed: LatencyMetric pinned 25, now 40.535"]


def test_unreadable_bound_zero_pushes():
    c = FakeClient(intent_doc={"expression": None})
    out = run(c)
    assert out["reasons"] == ["intent goal unreadable"] and c.posts == []
    assert "goal_changes" not in out


def test_last_outcome_recorded():
    c = FakeClient(post=[[rep("n", "2026-10-27T12:00:01Z")]])
    first = run(c)
    assert first["last_outcome"] is None
    assert src.TMF921_STORE.last_outcome(IID).value == "success"
    c2 = FakeClient(intent_doc=intent((URI, 50)))
    assert run(c2)["last_outcome"] == "success"
    assert src.TMF921_STORE.last_outcome(IID).value == "inconclusive"


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
                   post=[[rep("n", "2026-10-27T12:00:01Z"), rep("n2", "2026-10-27T12:00:02Z")]])
    out = run(c, bindings=BIND2)
    assert out["verdict"] == "success" and len(c.posts) == 2


def test_two_metrics_one_stale_never_success():
    def join(device, interface, metric, window, **kw):
        age = 1000 if metric == "jitter_ms" else 5
        return build_observed(f"{device}:{interface}", 1.0, "vm", NOW - timedelta(seconds=age), NOW)
    c = FakeClient(intent_doc=intent((URI, 100), (URI2, 10)),
                   post=[[rep("n", "2026-10-27T12:00:01Z")]])
    out = run(c, join, bindings=BIND2)
    assert out["verdict"] == "inconclusive" and c.posts == [] and c.lists == 0
    assert out["reasons"] == ["stale observation: JitterMetric"]


def test_two_metrics_failure_wins():
    c = FakeClient(intent_doc=intent((URI, 100), (URI2, 10)),
                   post=[[rep("n", "2026-10-27T12:00:01Z", "Degraded",
                              "Conditions not met: atLeast: JitterMetric: FAIL"),
                          rep("n2", "2026-10-27T12:00:00Z")]])
    assert run(c, bindings=BIND2)["verdict"] == "failure"


def test_extra_intent_metric_not_in_binding_is_drift():
    c = FakeClient(intent_doc=intent((URI, 100), (URI2, 10)),
                   post=[[rep("n", "2026-10-27T12:00:01Z")]])
    out = run(c)
    assert out["verdict"] == "inconclusive" and out["reasons"] == ["goal set changed"]
    assert c.posts == []


def test_missing_intent_metric_is_drift():
    c = FakeClient(intent_doc=intent((URI, 100)))
    out = run(c, bindings=BIND2)
    assert out["reasons"] == ["goal set changed"] and c.posts == []


def test_two_pushes_wait_for_two_new_reports():
    r1 = rep("r1", "2026-10-27T12:00:01Z")
    r2 = rep("r2", "2026-10-27T12:00:02Z", "Degraded",
             "Conditions not met: atLeast: JitterMetric: FAIL")
    c = FakeClient(intent_doc=intent((URI, 100), (URI2, 10)), post=[[r1], [r2, r1]])
    out = run(c, bindings=BIND2)
    assert out["verdict"] == "failure"
    assert len(out["_sleeps"]) == 1


def test_creation_date_ordering_is_not_string_based():
    # as strings "...01Z" > "...01.5Z"; as instants 01.5 is newer
    a = rep("a", "2026-10-27T12:00:01Z")
    b = rep("b", "2026-10-27T12:00:01.5Z", "Degraded",
            "Conditions not met: atLeast: LatencyMetric: FAIL")
    c = FakeClient(post=[[a, b]])
    assert run(c)["verdict"] == "failure"


def test_report_without_id_or_bad_date_is_not_new():
    bad = [dict(rep("x", "garbage")), {"creationDate": "2026-10-27T12:00:01Z",
                                        "intentHandlingState": "Fulfilled"}]
    c = FakeClient(post=[bad])
    out = run(c, poll_timeout_s=1.0, poll_interval_s=1.0)
    assert out["reasons"] == ["no new report"]


def test_goal_change_before_success_is_inconclusive():
    c = FakeClient(docs=[intent((URI, 100)), intent((URI, 50))],
                   post=[[rep("n", "2026-10-27T12:00:01Z")]])
    out = run(c)
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["goal changed: LatencyMetric pinned 100, now 50"]
    assert c.gets == 2


def test_bound_changed_after_success_report_names_metric():
    c = FakeClient(docs=[intent((URI, 100)), intent((URI, 40.535))],
                   post=[[rep("n", "2026-10-27T12:00:01Z")]])
    out = run(c)
    assert out["verdict"] == "inconclusive" and out["intent_status"] == "unknown"
    assert out["reasons"] == ["goal changed: LatencyMetric pinned 100, now 40.535"]
    assert out["goal_changes"][0]["current"] == 40.535
    assert len(c.posts) == 1


def test_same_intent_runs_are_serialised():
    entered, release = threading.Event(), threading.Event()

    class Blocking(FakeClient):
        def get_intent(self, i):
            self.gets += 1
            if self.gets == 1:
                entered.set()
                release.wait(5)
            return self.intent_doc

    c = Blocking(post=[[rep("n", "2026-10-27T12:00:01Z")]])
    results = []

    def work():
        results.append(assess_via_tmf921(IID, "5m", client=c, join=make_join(), now=NOW,
                                         metric_map=MMAP, bindings=BIND, sleep=lambda s: None,
                                         poll_timeout_s=0.0, monotonic=lambda: 0.0))
    t1 = threading.Thread(target=work)
    t1.start()
    assert entered.wait(5)
    t2 = threading.Thread(target=work)
    t2.start()
    t2.join(0.3)
    assert c.gets == 1 and t2.is_alive()
    release.set()
    t1.join(5)
    t2.join(5)
    assert len(results) == 2


def test_older_observation_than_last_pushed_rejected():
    c = FakeClient(post=[[rep("n", "2026-10-27T12:00:01Z")]])
    assert run(c, make_join(age=10))["verdict"] == "success"
    c2 = FakeClient(post=[[rep("m", "2026-10-27T12:00:02Z")]])
    out = run(c2, make_join(age=60))
    assert out["verdict"] == "inconclusive" and c2.posts == [] and c2.lists == 0
    assert out["reasons"] == ["observation not newer than last pushed: LatencyMetric"]


def test_equal_observation_different_value_rejected_same_value_allowed():
    c = FakeClient(post=[[rep("n", "2026-10-27T12:00:01Z")]])
    run(c, make_join(age=10, value=42.0))
    c2 = FakeClient(post=[[rep("m", "2026-10-27T12:00:02Z")]])
    assert run(c2, make_join(age=10, value=43.0))["verdict"] == "inconclusive"
    assert c2.posts == []
    c3 = FakeClient(post=[[rep("q", "2026-10-27T12:00:03Z")]])
    assert run(c3, make_join(age=10, value=42.0))["verdict"] == "success"


def test_marks_survive_a_restart_and_an_older_push_is_still_refused():
    from broker_state.process import reset_store
    run(FakeClient(post=[[rep("n", "2026-10-27T12:00:01Z")]]), make_join(age=10, value=42.0))
    reset_store()  # what an API restart does
    # same observation time, a different value
    c = FakeClient(post=[[rep("m", "2026-10-27T12:00:02Z")]])
    out = run(c, make_join(age=10, value=43.0))
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["observation not newer than last pushed: LatencyMetric"]
    assert c.posts == [] and c.lists == 0
    assert out["last_outcome"] == "success"
    # an older observation
    c2 = FakeClient(post=[[rep("p", "2026-10-27T12:00:02Z")]])
    out = run(c2, make_join(age=20))
    assert out["reasons"] == ["observation not newer than last pushed: LatencyMetric"]
    assert c2.posts == []
    # the same observation time and value is still pushed
    c3 = FakeClient(post=[[rep("q", "2026-10-27T12:00:03Z")]])
    assert run(c3, make_join(age=10, value=42.0))["verdict"] == "success"
    assert len(c3.posts) == 1


def test_a_failed_first_post_leaves_no_mark():
    run(FakeClient(push_exc=Tmf921Error("x")), make_join(age=10))
    assert src.TMF921_STORE.mark_count() == 0


def test_one_marks_read_covers_all_of_the_intents_metrics(monkeypatch):
    calls = []
    real = src.TMF921_STORE.marks

    def spy(intent_id, uris):
        calls.append((intent_id, list(uris)))
        return real(intent_id, uris)
    monkeypatch.setattr(src.TMF921_STORE, "marks", spy)
    c = FakeClient(intent_doc=intent((URI, 100), (URI2, 10)),
                   post=[[rep("n", "2026-10-27T12:00:01Z"), rep("m", "2026-10-27T12:00:02Z")]])
    run(c, bindings=BIND2)
    assert calls == [(IID, [URI, URI2])]


def test_an_unadvanced_mark_is_logged_and_the_assess_continues(monkeypatch, caplog):
    monkeypatch.setattr(src.TMF921_STORE, "advance_mark", lambda *a, **k: False)
    c = FakeClient(post=[[rep("n", "2026-10-27T12:00:01Z")]])
    with caplog.at_level("WARNING"):
        out = run(c)
    assert out["verdict"] == "success" and len(c.posts) == 1
    assert "high-water mark not advanced: LatencyMetric" in caplog.text


def test_mark_not_updated_when_push_fails():
    run(FakeClient(push_exc=Tmf921Error("x")), make_join(age=10))
    assert src.TMF921_STORE.mark_count() == 0


def test_no_binding_and_invalid_binding_not_recorded(monkeypatch):
    run(FakeClient(), bindings={})
    assert src.TMF921_STORE.tracked_count() == 0
    monkeypatch.setattr(src, "resolve_binding", lambda i: (_ for _ in ()).throw(ValueError("x")))
    assert_out = assess_via_tmf921(IID, "5m", client=FakeClient(), metric_map=MMAP)
    assert assert_out["reasons"] == ["invalid binding"]
    assert src.TMF921_STORE.tracked_count() == 0


def test_full_outcome_table_evicts_the_oldest_and_says_so(monkeypatch, caplog):
    from broker_state.process import reset_store
    from outcome_assurance.evaluate import Outcome
    monkeypatch.setenv("BROKER_OUTCOME_CAP", "1")
    reset_store()
    run(FakeClient(post=[[rep("n", "2026-10-27T12:00:01Z")]]))
    assert src.TMF921_STORE.last_outcome(IID) is Outcome.SUCCESS
    b = {"other": IntentBinding("other", {URI: ("olt1", "pon0/1")}, {URI: 100.0})}
    with caplog.at_level("INFO"):
        run(FakeClient(), bindings=b, iid="other")
    assert src.TMF921_STORE.tracked_count() == 1
    assert src.TMF921_STORE.last_outcome("other") is not None
    assert src.TMF921_STORE.last_outcome(IID) is None
    lines = [r.getMessage() for r in caplog.records if "state eviction" in r.getMessage()]
    assert lines == ["state eviction: table=outcomes evicted=1"]
    assert "other" not in lines[0] and IID not in lines[0]


def test_last_outcome_is_reported_after_the_store_is_reopened():
    from broker_state.process import reset_store
    from outcome_assurance.evaluate import Outcome
    run(FakeClient(post=[[rep("n", "2026-10-27T12:00:01Z")]]), make_join(age=10))
    assert src.TMF921_STORE.last_outcome(IID) is Outcome.SUCCESS
    reset_store()  # what an API restart does
    c = FakeClient(post=[[rep("m", "2026-10-27T12:00:02Z")]])
    out = run(c, make_join(age=5))
    assert out["last_outcome"] == "success"
    assert out["verdict"] == "success"


def test_unknown_status_for_no_binding_and_drift():
    assert run(FakeClient(), bindings={})["intent_status"] == "unknown"
    assert run(FakeClient(intent_doc=intent((URI, 5))))["intent_status"] == "unknown"


def test_observed_at_reaches_interpret_stale_cap():
    c = FakeClient(post=[[rep("n", "2026-10-27T12:00:01Z")]])
    out = assess_via_tmf921(IID, "5m", client=c, join=make_join(age=290),
                            now=NOW + timedelta(seconds=20), metric_map=MMAP,
                            bindings=BIND, sleep=lambda s: None, monotonic=lambda: 0.0)
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["stale observation: LatencyMetric"]


def test_join_client_input_error_is_invalid_binding():
    def join(*a, **kw):
        raise ClientInputError("bad")
    out = run(FakeClient(), join)
    assert out["reasons"] == ["invalid binding"]


def test_post_client_input_error_is_invalid_observation():
    out = run(FakeClient(push_exc=ClientInputError("bad")))
    assert out["reasons"] == ["invalid observation"]


def test_future_dated_envelope_not_pushed():
    c = FakeClient()
    out = run(c, make_join(age=-60))
    assert c.posts == [] and out["reasons"] == ["future-dated observation: LatencyMetric"]


def test_real_deadline_bounds_polling():
    t = [0.0]

    def mono():
        t[0] += 10.0
        return t[0]
    c = FakeClient(pre=[OLD_OK], post=[[OLD_OK]])
    out = run(c, monotonic=mono, poll_timeout_s=5.0, poll_interval_s=0.1)
    assert out["reasons"] == ["no new report"] and len(out["_sleeps"]) == 0


R_DEG = "Conditions not met: atLeast: JitterMetric: FAIL"


def two(post, pre=()):
    return FakeClient(intent_doc=intent((URI, 100), (URI2, 10)), pre=pre, post=post)


def test_garbage_old_date_cannot_reorder_new_reports():
    old = rep("old", "garbage")
    r1 = rep("r1", "2026-10-27T12:00:01Z")
    r2 = rep("r2", "2026-10-27T12:00:02Z", "Degraded", R_DEG)
    out = run(two([[old, r1, r2]], pre=[old]), bindings=BIND2)
    assert out["verdict"] == "failure"


def test_tie_at_top_is_ambiguous():
    r1 = rep("r1", "2026-10-27T12:00:01Z")
    r2 = rep("r2", "2026-10-27T12:00:01Z", "Degraded", R_DEG)
    out = run(two([[r1, r2]]), bindings=BIND2)
    assert out["verdict"] == "inconclusive" and out["reasons"] == ["ambiguous report order"]


def test_identical_dates_server_order_never_success():
    r1 = rep("r1", "2026-10-27T12:00:01.5Z")
    r2 = rep("r2", "2026-10-27T12:00:01.500Z", "Degraded", R_DEG)
    out = run(two([[r1, r2]]), bindings=BIND2)
    assert out["verdict"] != "success"


def test_offset_date_is_converted_to_utc():
    a = rep("a", "2026-10-27T12:00:01Z")
    b = rep("b", "2026-10-27T13:00:02+01:00", "Degraded",
            "Conditions not met: atLeast: LatencyMetric: FAIL")
    assert run(FakeClient(post=[[a, b]]), poll_timeout_s=0.0)["verdict"] == "failure"
    assert run(FakeClient(post=[[b, a]]), poll_timeout_s=0.0)["verdict"] == "failure"


def test_naive_date_is_not_new():
    out = run(FakeClient(post=[[rep("n", "2026-10-27T12:00:01")]]), poll_timeout_s=0.0)
    assert out["reasons"] == ["no new report"]


def test_oldest_first_server_list_picks_true_newest():
    r1 = rep("r1", "2026-10-27T12:00:01Z")
    r2 = rep("r2", "2026-10-27T12:00:02Z", "Degraded", R_DEG)
    assert run(two([[r1, r2]]), bindings=BIND2)["verdict"] == "failure"


def test_input_rejection_paths_not_recorded_and_unknown():
    def join(*a, **kw):
        raise ClientInputError("bad")
    for out in (run(FakeClient(), join),
                run(FakeClient(get_exc=ClientInputError("bad"))),
                run(FakeClient(push_exc=ClientInputError("bad")))):
        assert out["verdict"] == "inconclusive" and out["intent_status"] == "unknown"
        assert src.TMF921_STORE.tracked_count() == 0


def test_join_asked_for_last_sample():
    join = make_join()
    run(FakeClient(post=[[rep("n", "2026-10-27T12:00:01Z")]]), join)
    assert join.kw == {"aggregate": "last"}


def test_stale_high_window_value_not_pushed():
    """Window max 150 (20 min ago) vs latest 50: a joiner honouring aggregate='last'
    returns 50; the old high is never pushed."""
    def join(device, interface, metric, window, aggregate="max"):
        value = 50.0 if aggregate == "last" else 150.0
        return build_observed(f"{device}:{interface}", value, "vm", NOW - timedelta(seconds=60), NOW)
    c = FakeClient(post=[[rep("n", "2026-10-27T12:00:01Z", "Degraded",
                              "Conditions not met: atLeast: LatencyMetric: FAIL")]])
    out = run(c, join)
    assert c.posts[0][1] == 50.0 and out["verdict"] == "failure"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "x", True])
def test_non_finite_second_metric_means_zero_posts(bad):
    def join(device, interface, metric, window, **kw):
        v = bad if metric == "jitter_ms" else 1.0
        return build_observed(f"{device}:{interface}", v, "vm", NOW - timedelta(seconds=5), NOW)
    c = two([[rep("a", "2026-10-27T12:00:01Z"), rep("b", "2026-10-27T12:00:02Z")]])
    out = run(c, join, bindings=BIND2)
    assert c.posts == [] and out["verdict"] == "inconclusive"


# --- binding resolution: network graph only ---

GRAPH_BIND = IntentBinding(IID, {URI: ("olt9", "pon9/9")}, {URI: 100.0})


def _assess(c):
    return assess_via_tmf921(IID, "5m", client=c, join=make_join(), now=NOW,
                             metric_map=MMAP, sleep=lambda s: None,
                             monotonic=lambda: 0.0)


def test_binding_store_down_is_inconclusive_and_pushes_nothing(monkeypatch):
    def down(*a, **k):
        raise requests.ConnectionError("fuseki says: secret detail")
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding", down)
    c = FakeClient()
    out = _assess(c)
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["binding store unavailable"]
    assert out["intent_status"] == "unknown"
    assert "secret" not in str(out)
    assert c.posts == [] and c.gets == 0


def test_binding_store_down_has_no_other_binding_source(monkeypatch):
    def down(*a, **k):
        raise requests.ConnectionError("x")
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding", down)
    assert src._binding_for(IID) == (None, "binding store unavailable")


def test_non_json_graph_reply_is_binding_store_unavailable(monkeypatch):
    def down(*a, **k):
        raise requests.exceptions.JSONDecodeError("not json", "<html>", 0)
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding", down)
    assert src._binding_for(IID) == (None, "binding store unavailable")


def test_graph_binding_is_used(monkeypatch):
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding",
                        lambda i: GRAPH_BIND)
    assert src._binding_for(IID) == (GRAPH_BIND, None)
    c = FakeClient()
    _assess(c)
    assert c.posts and c.posts[0][0] == URI


def test_no_binding_in_graph(monkeypatch):
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding",
                        lambda i: None)
    assert src._binding_for(IID) == (None, "no binding for intent")
    c = FakeClient()
    out = _assess(c)
    assert out["reasons"] == ["no binding for intent"] and c.posts == [] and c.gets == 0


def test_malformed_graph_binding_is_invalid_and_pushes_nothing(monkeypatch):
    def bad(*a, **k):
        raise ValueError("duplicate")
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding", bad)
    assert src._binding_for(IID) == (None, "invalid binding")
    c = FakeClient()
    out = _assess(c)
    assert out["reasons"] == ["invalid binding"] and c.posts == []


def test_explicit_bindings_never_touch_the_graph(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("graph consulted")
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding", boom)
    assert src._binding_for(IID, BIND) == (BIND[IID], None)
    assert src._binding_for(IID, {}) == (None, "no binding for intent")


def test_unsafe_intent_id_propagates_from_binding_for(monkeypatch):
    from broker_engine.binding_resolver import resolve_binding
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding", resolve_binding)
    with pytest.raises(ClientInputError):
        src._binding_for("bad id!")


def test_no_json_bindings_fallback():
    import data_contracts.intent_bindings as bindings_module
    assert not hasattr(bindings_module, "load_bindings")
    assert not hasattr(src, "load_bindings")
    assert hasattr(bindings_module, "IntentBinding")


# ---------- a push that fails part-way (D-14, D-15) ----------
class SecondPostFails(FakeClient):
    """Accepts the first post, raises on every later one."""

    def __init__(self, exc, **kw):
        super().__init__(**kw)
        self.exc = exc

    def post_observation(self, i, uri, value, unit=None, obtained_at=None):
        if self.posts:
            raise self.exc
        super().post_observation(i, uri, value, unit, obtained_at)


@pytest.mark.parametrize("exc", [Tmf921Error("server says SECRET"),
                                 ClientInputError("SECRET value")])
def test_second_push_failure_is_a_partial_push(exc):
    c = SecondPostFails(exc, intent_doc=intent((URI, 100), (URI2, 10)))
    out = run(c, bindings=BIND2)
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == ["partial push: pushed LatencyMetric; failed JitterMetric"]
    assert len(c.posts) == 1 and c.posts[0][0] == URI  # nothing retried, nothing further
    assert c.lists == 1  # no polling after the failure
    assert set(src.TMF921_STORE.marks(IID, [URI, URI2])) == {URI}
    assert "SECRET" not in str(out)
    assert src.TMF921_STORE.last_outcome(IID).value == "inconclusive"


# ---------- gaps: a goal or placement missing at the requested time ----------

GAP_BIND = IntentBinding(IID, {URI: ("olt1", "pon0/1")}, {URI: 100.0},
                         gaps=((URI2, "goal"), (URI2, "placement")))
GAP_REASONS = ["no goal in force: JitterMetric", "no placement in force: JitterMetric"]


def test_gap_reasons_are_fixed_strings_from_the_pinned_metric_names():
    assert src._gap_reasons(GAP_BIND) == GAP_REASONS
    assert src._gap_reasons(IntentBinding(IID, {}, {})) == []
    only = IntentBinding(IID, {}, {}, gaps=((URI, "placement"),))
    assert src._gap_reasons(only) == ["no placement in force: LatencyMetric"]


def _nothing_happened(c, join):
    assert (c.gets, c.lists, c.posts) == (0, 0, [])
    assert join.seen == []
    assert src.TMF921_STORE.mark_count() == 0
    assert src.TMF921_STORE.tracked_count() == 0
    assert src.TMF921_STORE.last_outcome(IID) is None


def test_a_gap_makes_the_push_assess_inconclusive_before_any_tmf921_call():
    c, join = FakeClient(), make_join()
    out = run(c, join, bindings={IID: GAP_BIND})
    assert out["verdict"] == "inconclusive"
    assert out["intent_status"] == "unknown"
    assert out["reasons"] == GAP_REASONS
    assert out["source"] == "tmf921"
    _nothing_happened(c, join)


def test_a_gap_from_the_graph_makes_the_push_assess_inconclusive(monkeypatch):
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding", lambda i: GAP_BIND)
    c, join = FakeClient(), make_join()
    out = assess_via_tmf921(IID, "5m", client=c, join=join, now=NOW, metric_map=MMAP,
                            sleep=lambda s: None, monotonic=lambda: 0.0)
    assert out["verdict"] == "inconclusive" and out["reasons"] == GAP_REASONS
    _nothing_happened(c, join)


def test_a_gap_is_checked_before_the_metric_map_is_read(monkeypatch):
    def boom():
        raise AssertionError("metric map read")
    monkeypatch.setattr("intent_management.tmf921_source.load_metric_map", boom)
    out = assess_via_tmf921(IID, "5m", client=FakeClient(), join=make_join(), now=NOW,
                            bindings={IID: GAP_BIND}, sleep=lambda s: None,
                            monotonic=lambda: 0.0)
    assert out["reasons"] == GAP_REASONS


def test_a_gap_stays_inconclusive_even_when_the_covered_metric_would_succeed():
    # LatencyMetric is covered and fine; JitterMetric has no goal: the whole result is inconclusive.
    c = FakeClient(pre=[OLD], post=[[rep("new", "2026-10-27T12:00:01Z")]])
    out = run(c, make_join(age=10), bindings={IID: GAP_BIND})
    assert out["verdict"] != "success"
    assert c.posts == []


def test_a_binding_without_gaps_behaves_as_before():
    c = FakeClient(pre=[OLD], post=[[rep("new", "2026-10-27T12:00:01Z")]])
    out = run(c, make_join(age=10))
    assert out["verdict"] == "success"
    assert c.posts


AS_OF_NOW = NOW


def _as_of(c, join, bindings, **kw):
    return src.assess_tmf921_as_of(IID, "5m", as_of=kw.pop("as_of", None),
                                   mode=kw.pop("mode", "within"), client=c, join=join,
                                   now=AS_OF_NOW, metric_map=MMAP, bindings=bindings)


@pytest.mark.parametrize("as_of,mode", [(None, "within"), ("10m", "at"), (None, None)])
def test_a_gap_makes_the_temporal_assess_inconclusive_with_zero_tmf921_calls(as_of, mode):
    c, join = FakeClient(), make_join()
    out = _as_of(c, join, {IID: GAP_BIND}, as_of=as_of, mode=mode)
    assert out["verdict"] == "inconclusive"
    assert out["reasons"] == GAP_REASONS
    assert out["intent_status"] == "unknown"
    assert out["mode"] == (mode or "at")
    _nothing_happened(c, join)


def test_a_gap_from_the_graph_makes_the_temporal_assess_inconclusive(monkeypatch):
    monkeypatch.setattr("intent_management.tmf921_source.resolve_binding", lambda i: GAP_BIND)
    c, join = FakeClient(), make_join()
    out = _as_of(c, join, None)
    assert out["reasons"] == GAP_REASONS
    _nothing_happened(c, join)


def test_a_binding_without_gaps_is_assessed_by_the_temporal_path_as_before():
    c = FakeClient(intent_doc={**intent((URI, 100)), "lifecycleStatus": "ACTIVE"})
    mmap = {URI: MetricMapping("latency_ms", URI, "ms", "<")}
    out = src.assess_tmf921_as_of(IID, "5m", as_of=None, mode="within", client=c,
                                  join=make_join(age=10), now=NOW, metric_map=mmap,
                                  bindings=BIND)
    assert out["verdict"] == "success"
    assert c.gets == 1 and c.posts == []
