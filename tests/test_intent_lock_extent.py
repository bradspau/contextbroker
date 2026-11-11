"""What runs under the per-intent lock of assess_via_tmf921, and what does not.

The binding read, the goal check, the mapping check, the telemetry reads and the
pushability check happen before the lock. Under it: the previous outcome, the marks, the
goal guard, the freshness re-check, the snapshot, the pushes, the poll, the success
re-check and the outcome write. A spy proves it on the success path and on every early exit.
"""
import time
from datetime import timedelta

import pytest

from broker_engine.telemetry_reads import read_in_order
from data_contracts.envelope import build_observed
from data_contracts.intent_bindings import IntentBinding
from data_contracts.metric_map import MetricMapping
from intent_management import tmf921_source as src
from intent_management.tmf921_client import Tmf921Error
from intent_management.tmf921_source import TMF921_STORE, assess_via_tmf921
from test_tmf921_source import (  # noqa: F401  (fixtures and helpers)
    BIND2, IID, MMAP, NOW, URI, URI2, FakeClient, clean_store, intent, make_join,
    no_live_graph, rep,
)

URI3 = "http://broadband-forum.org/Intent#PacketLossMetric"
MMAP3 = {**MMAP, URI3: MetricMapping("packet_loss_pct", URI3, "%")}
BIND3 = {IID: IntentBinding(
    IID, {URI: ("olt1", "pon0/1"), URI2: ("olt1", "pon0/2"), URI3: ("olt1", "pon0/3")},
    {URI: 100.0, URI2: 10.0, URI3: 1.0})}


class Probe:
    """Wraps the real per-intent lock; `held` is true exactly while an assess holds it."""

    def __init__(self, monkeypatch):
        self.held = False
        self.calls = []  # (name, held at call time)
        real = src._intent_lock
        probe = self

        class Held:
            def __init__(self, lock):
                self.lock = lock

            def __enter__(self):
                self.lock.acquire()
                probe.held = True
                return self

            def __exit__(self, *exc):
                probe.held = False
                self.lock.release()

        monkeypatch.setattr(src, "_intent_lock", lambda iid: Held(real(iid)))

    def see(self, name):
        self.calls.append((name, self.held))

    def held_at(self, name):
        return [h for n, h in self.calls if n == name]

    def join(self, inner):
        def spy(*a, **k):
            self.see("join")
            return inner(*a, **k)
        spy.seen = inner.seen
        return spy

    def client(self, base):
        probe = self

        class Spied(type(base)):
            def get_intent(self, i):
                probe.see("get_intent")
                return super().get_intent(i)

            def list_reports(self, i):
                probe.see("list_reports")
                return super().list_reports(i)

            def post_observation(self, *a, **k):
                probe.see("post_observation")
                return super().post_observation(*a, **k)

        base.__class__ = Spied
        return base


@pytest.fixture
def probe(monkeypatch):
    p = Probe(monkeypatch)

    def resolve(iid):
        p.see("resolve_binding")
        return BIND2[iid]

    monkeypatch.setattr(src, "resolve_binding", resolve)
    return p


def _run(probe, client, join, **kw):
    sleeps = []
    return assess_via_tmf921(IID, "5m", client=probe.client(client), join=probe.join(join),
                             now=NOW, metric_map=MMAP, bindings=None, sleep=sleeps.append,
                             monotonic=lambda: 0.0, **kw)


def test_success_path_reads_outside_the_lock_and_pushes_inside(probe):
    c = FakeClient(docs=[intent((URI, 100), (URI2, 10))] * 3,
                   post=[[rep("a", "2026-10-27T12:00:01Z"), rep("b", "2026-10-27T12:00:02Z")]])
    out = _run(probe, c, make_join())
    assert out["verdict"] == "success"
    assert probe.held_at("resolve_binding") == [False]
    assert probe.held_at("join") == [False, False]
    assert probe.held_at("get_intent") == [False, True, True]  # goal check, guard, re-check
    assert probe.held_at("list_reports") == [True, True]  # snapshot, poll
    assert probe.held_at("post_observation") == [True, True]
    assert len(c.posts) == 2 and [p[0] for p in c.posts] == [URI, URI2]


def test_success_path_the_lock_is_released_afterwards(probe):
    c = FakeClient(docs=[intent((URI, 100), (URI2, 10))] * 3,
                   post=[[rep("a", "2026-10-27T12:00:01Z"), rep("b", "2026-10-27T12:00:02Z")]])
    _run(probe, c, make_join())
    assert probe.held is False


GOAL = intent((URI, 100), (URI2, 10))


def _early(name):
    """(client, join, expected reason fragment) for each way to end before any push."""
    if name == "goal changed":
        return FakeClient(intent_doc=intent((URI, 50), (URI2, 10))), make_join(), "goal changed"
    if name == "unmapped metric":
        return FakeClient(intent_doc=GOAL), make_join(), "unmapped metric"
    if name == "missing telemetry":
        return FakeClient(intent_doc=GOAL), make_join(missing=True), "missing observation"
    if name == "stale telemetry":
        return FakeClient(intent_doc=GOAL), make_join(age=1000), "stale observation"
    if name == "conflicting telemetry":
        return FakeClient(intent_doc=GOAL), make_join(conflicts=True), "conflicting observation"
    return FakeClient(get_exc=Tmf921Error("down")), make_join(), "intent server unavailable"


@pytest.mark.parametrize("name", ["goal changed", "unmapped metric", "missing telemetry",
                                  "stale telemetry", "conflicting telemetry",
                                  "intent server unavailable"])
def test_early_exits_read_nothing_under_the_lock_and_still_record(probe, name):
    c, join, fragment = _early(name)
    mmap = {URI: MMAP[URI]} if name == "unmapped metric" else MMAP
    sleeps = []
    out = assess_via_tmf921(IID, "5m", client=probe.client(c), join=probe.join(join), now=NOW,
                            metric_map=mmap, bindings=None, sleep=sleeps.append,
                            monotonic=lambda: 0.0)
    assert out["verdict"] == "inconclusive"
    assert any(fragment in r for r in out["reasons"])
    assert probe.held_at("resolve_binding") == [False]
    assert all(h is False for h in probe.held_at("join"))
    assert all(h is False for h in probe.held_at("get_intent"))
    assert probe.held_at("post_observation") == [] and c.posts == []
    assert TMF921_STORE.last_outcome(IID).value == out["verdict"]


def test_stale_telemetry_reads_happen_before_the_lock_even_with_two_metrics(probe):
    c = FakeClient(intent_doc=GOAL)
    _run(probe, c, make_join(age=1000))
    assert probe.held_at("join") == [False, False]


def test_a_changed_goal_stops_the_assess_before_any_telemetry_read(probe):
    c = FakeClient(intent_doc=intent((URI, 50), (URI2, 10)))
    join = make_join()
    _run(probe, c, join)
    assert join.seen == []


def test_results_are_the_same_with_one_worker_and_eight(monkeypatch, tmp_path):
    def keyed_join(device, interface, metric, window, **kw):
        # distinct value and age per (device, interface, metric); the first metric is slowest
        time.sleep({"latency_ms": 0.06, "jitter_ms": 0.03}.get(metric, 0.0))
        value = {"latency_ms": 42.0, "jitter_ms": 3.5, "packet_loss_pct": 0.25}[metric]
        age = {"latency_ms": 10, "jitter_ms": 20, "packet_loss_pct": 30}[metric]
        return build_observed(f"{device}:{interface}", value, "victoriametrics",
                              NOW - timedelta(seconds=age), NOW)

    results = {}
    posts = {}
    for workers in (1, 8):
        from broker_state.process import reset_store
        monkeypatch.setenv("BROKER_STATE_PATH", str(tmp_path / f"state{workers}.db"))
        reset_store()  # a fresh state file per run, so last_outcome starts empty in both
        monkeypatch.setattr(src, "read_in_order",
                            lambda fn, items, w=workers: read_in_order(fn, items, w))
        docs = [intent((URI, 100), (URI2, 10), (URI3, 1))] * 3
        c = FakeClient(docs=docs, post=[[rep(f"r{i}", f"2026-10-27T12:00:0{i}Z")
                                         for i in (1, 2, 3)]])
        results[workers] = assess_via_tmf921(
            IID, "5m", client=c, join=keyed_join, now=NOW, metric_map=MMAP3, bindings=BIND3,
            sleep=lambda s: None, monotonic=lambda: 0.0)
        posts[workers] = [p[0] for p in c.posts]
    assert results[1] == results[8]
    assert results[1]["verdict"] == "success"
    assert posts[1] == posts[8] == [URI, URI2, URI3]
    assert [e["entity_id"] for e in results[1]["context"]] == \
        ["olt1:pon0/1", "olt1:pon0/2", "olt1:pon0/3"]
