"""Operations queries: metric history, non-zero interfaces, interface neighbours."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from broker_engine import operations as ops
from broker_engine.validation import ClientInputError

NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
T = NOW.timestamp()


def _resp(payload):
    r = Mock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


class FakeVM:
    """Answers instant queries by function name. series: {fn: [(labels, value), ...]}"""

    def __init__(self, series):
        self.series, self.calls = series, []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append(params)
        q = params["query"]
        fn = "tlast_over_time" if "tlast_over_time" in q else q.split("(")[0]
        rows = self.series.get(fn, [])
        return _resp({"data": {"result": [{"metric": lab, "value": [T, str(v)]} for lab, v in rows]}})


A = {"device": "r1", "interface": "e0"}


def hist(extra=None, **kw):
    series = {"min_over_time": [(A, 0)], "max_over_time": [(A, 9)], "last_over_time": [(A, 0)],
              "count_over_time": [(A, 120)], "tlast_over_time": [(A, T - 10)]}
    series.update(extra or {})
    vm = FakeVM(series)
    with patch("broker_engine.operations.requests.get", vm):
        out = ops.metric_history("r1", "e0", "input_drops", kw.pop("window", "30m"), now=NOW, **kw)
    return out, vm


# ---------- metric_history ----------
def test_history_reports_min_max_last_and_samples():
    out, _ = hist()
    assert (out["min"], out["max"], out["last"], out["samples"]) == (0.0, 9.0, 0.0, 120)
    assert out["entity_id"] == "r1:e0" and out["state"] == "observed"
    assert out["quality"] == {"stale": False, "missing": False, "conflicts": []}
    assert out["observed_at"] == datetime.fromtimestamp(T - 10, timezone.utc).isoformat()


def test_history_as_of_queries_the_past_and_judges_freshness_there():
    at = NOW - timedelta(minutes=10)
    out, vm = hist({"tlast_over_time": [(A, at.timestamp() - 20)]}, as_of="10m")
    assert all(float(c["time"]) == at.timestamp() for c in vm.calls)
    assert out["evaluated_at"] == at.isoformat() and out["quality"]["stale"] is False


def test_history_no_time_param_without_as_of():
    _, vm = hist()
    assert all("time" not in c for c in vm.calls)


def test_history_no_data_is_missing_not_zero():
    out, _ = hist({"min_over_time": []})
    assert out["quality"]["missing"] is True
    assert out["min"] is None and out["max"] is None and out["last"] is None and out["samples"] == 0


def test_history_old_last_sample_is_stale():
    out, _ = hist({"tlast_over_time": [(A, T - 900)]})
    assert out["quality"]["stale"] is True and out["max"] == 9.0  # data still returned


@pytest.mark.parametrize("ts", [[], [(A, "nan")], [(A, T + 3600)]])
def test_history_unknown_or_future_sample_time_is_stale(ts):
    out, _ = hist({"tlast_over_time": ts})
    assert out["quality"]["stale"] is True and out["observed_at"] is None


def test_history_multiple_series_is_a_conflict_and_last_is_withheld():
    b = {"device": "r1", "interface": "e0", "x": "2"}
    out, _ = hist({"min_over_time": [(A, 0), (b, -2)], "max_over_time": [(A, 9), (b, 1)],
                   "last_over_time": [(A, 0), (b, 1)]})
    assert out["min"] == -2.0 and out["max"] == 9.0 and out["last"] is None
    assert any("multiple series" in c for c in out["quality"]["conflicts"])


def test_history_non_finite_values_are_flagged_not_returned():
    out, _ = hist({"max_over_time": [(A, "inf")]})
    assert out["max"] is None
    assert any("non-finite" in c for c in out["quality"]["conflicts"])


@pytest.mark.parametrize("kw", [{"as_of": "PT5M"}, {"window": "PT1H"}, {"window": "5m\n"},
                                {"as_of": "99999d"}])
def test_history_rejects_bad_arguments(kw):
    with pytest.raises(ClientInputError):
        hist(**kw)


@pytest.mark.parametrize("bad", ['r1"}', "r1 ", "a b", "r1\n"])
def test_history_rejects_unsafe_identifiers(bad):
    with pytest.raises(ClientInputError):
        ops.metric_history(bad, "e0", "input_drops", "5m", now=NOW)
    with pytest.raises(ClientInputError):
        ops.metric_history("r1", bad, "input_drops", "5m", now=NOW)
    with pytest.raises(ClientInputError):
        ops.metric_history("r1", "e0", bad, "5m", now=NOW)


# ---------- interfaces_with_nonzero ----------
def lab(i):
    return {"device": "r1", "interface": i}


def nz(series, **kw):
    vm = FakeVM(series)
    with patch("broker_engine.operations.requests.get", vm):
        out = ops.interfaces_with_nonzero("r1", "input_drops", kw.pop("window", "30m"),
                                          now=NOW, **kw)
    return out, vm


SERIES = {
    "max_over_time": [(lab("e0"), 0), (lab("e1"), 7), (lab("e2"), 0), (lab("e3"), 20)],
    "min_over_time": [(lab("e0"), 0), (lab("e1"), 0), (lab("e2"), -4), (lab("e3"), 20)],
    "last_over_time": [(lab("e0"), 0), (lab("e1"), 7), (lab("e2"), 0), (lab("e3"), 20)],
    "tlast_over_time": [(lab(i), T - 10) for i in ("e0", "e1", "e2", "e3")],
}


def test_nonzero_lists_only_nonzero_sorted_by_magnitude_and_counts_all():
    out, _ = nz(SERIES)
    assert [i["interface"] for i in out["interfaces"]] == ["e3", "e1", "e2"]  # 20, 7, |-4|
    assert out["interfaces_examined"] == 4 and out["truncated"] is False
    e2 = next(i for i in out["interfaces"] if i["interface"] == "e2")
    assert (e2["min"], e2["max"], e2["last"]) == (-4.0, 0.0, 0.0)  # negative-only counts
    assert out["quality"]["missing"] is False


def test_nonzero_all_zero_is_an_empty_list_not_missing():
    zero = {k: [(l, 0) for l, _ in v] for k, v in SERIES.items() if k != "tlast_over_time"}
    zero["tlast_over_time"] = SERIES["tlast_over_time"]
    out, _ = nz(zero)
    assert out["interfaces"] == [] and out["interfaces_examined"] == 4
    assert out["quality"]["missing"] is False


def test_nonzero_no_data_at_all_is_missing_not_all_zero():
    out, _ = nz({})
    assert out["quality"]["missing"] is True and out["interfaces_examined"] == 0


def test_nonzero_limit_truncates():
    out, _ = nz(SERIES, limit=2)
    assert [i["interface"] for i in out["interfaces"]] == ["e3", "e1"] and out["truncated"] is True


def test_nonzero_stale_interfaces_are_flagged_and_counted():
    s = dict(SERIES, tlast_over_time=[(lab("e0"), T - 9000), (lab("e1"), T - 9000),
                                      (lab("e2"), T - 10), (lab("e3"), T - 10)])
    out, _ = nz(s)
    assert out["stale_interfaces"] == 2
    assert {i["interface"]: i["stale"] for i in out["interfaces"]} == {"e1": True, "e2": False, "e3": False}


def test_nonzero_as_of_sends_time():
    at = NOW - timedelta(hours=1)
    _, vm = nz(SERIES, as_of="1h")
    assert all(float(c["time"]) == at.timestamp() for c in vm.calls)


def test_nonzero_non_finite_is_flagged_and_skipped():
    s = dict(SERIES, max_over_time=[(lab("e0"), "inf")] + SERIES["max_over_time"][1:])
    out, _ = nz(s)
    assert any("e0" in c and "non-finite" in c for c in out["quality"]["conflicts"])
    assert out["interfaces_examined"] == 3


@pytest.mark.parametrize("limit", [0, -1, 201, True, "5", 2.5])
def test_nonzero_rejects_bad_limit(limit):
    with pytest.raises(ClientInputError):
        nz(SERIES, limit=limit)


def test_nonzero_rejects_unsafe_input():
    for kw in ({"window": "PT1H"}, {"as_of": "x"}):
        with pytest.raises(ClientInputError):
            nz(SERIES, **kw)
    with pytest.raises(ClientInputError):
        ops.interfaces_with_nonzero('r1"}', "input_drops", "5m", now=NOW)


# ---------- interface_neighbours ----------
def _sparql(rows):
    def post(url, data=None, headers=None, timeout=None, auth=None):
        post.sent = data["query"]
        post.auth = auth
        return _resp({"results": {"bindings": rows}})
    return post


def _b(**kv):
    return {k: {"value": v} for k, v in kv.items()}


def neighbours(rows, device="Router-A", interface="GigabitEthernet0/0/0"):
    post = _sparql(rows)
    with patch("broker_engine.operations.requests.post", post):
        return ops.interface_neighbours(device, interface), post


def test_neighbours_across_a_link():
    out, post = neighbours([_b(link="http://x#l1", label="Router-A <-> Router-B core link",
                               otherDevice="Router-B", otherName="GigabitEthernet0/0/1")])
    assert out["neighbours"] == [{"device": "Router-B", "interface": "GigabitEthernet0/0/1",
                                  "link": "Router-A <-> Router-B core link"}]
    assert out["state"] == "configured" and out["source"] == "fuseki"
    assert out["quality"]["missing"] is False
    assert '"Router-A"' in post.sent and '"GigabitEthernet0/0/0"' in post.sent


def test_neighbours_known_interface_without_links_is_empty_not_missing():
    out, _ = neighbours([{}])  # row with the interface but no optional binding
    assert out["neighbours"] == [] and out["quality"]["missing"] is False


def test_neighbours_unknown_interface_is_missing():
    out, _ = neighbours([])
    assert out["neighbours"] == [] and out["quality"]["missing"] is True


def test_neighbours_dedupes_and_sorts_and_falls_back_to_link_uri():
    row = _b(link="http://x#l1", otherDevice="Router-C", otherName="e1")
    out, _ = neighbours([row, row, _b(link="http://x#l2", otherDevice="Router-B", otherName="e9")])
    assert [(n["device"], n["link"]) for n in out["neighbours"]] == [
        ("Router-B", "http://x#l2"), ("Router-C", "http://x#l1")]


@pytest.mark.parametrize("bad", ['Router-A"', "A B", "x\n", 'a" } UNION { ?s ?p ?o', ""])
def test_neighbours_rejects_unsafe_identifiers_before_any_request(bad):
    with patch("broker_engine.operations.requests.post") as post:
        with pytest.raises(ClientInputError):
            ops.interface_neighbours(bad, "e0")
        with pytest.raises(ClientInputError):
            ops.interface_neighbours("r1", bad)
    post.assert_not_called()


def test_neighbours_send_the_credential_when_set(monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "testpassword0")
    _, post = neighbours([])
    assert post.auth == ("admin", "testpassword0")


def test_neighbours_send_no_credential_when_unset(monkeypatch):
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
    _, post = neighbours([])
    assert post.auth is None


# ---------- interface_neighbours over link fluents (TEMP-04, TEMP-05) ----------
def _topology():
    """Router-A/ge0 linked to Router-B/ge1 for [T0, T1) and to Router-C/ge2 from T1; the
    A-C link is corrected at T2 to A-D (superseded and reissued with the same interval)."""
    import rdflib

    from data_contracts import fluents as f
    from tests.fluent_helpers import NS, T0, T1, T2, GraphFuseki
    g = rdflib.Graph()
    g.parse(data=f"""@prefix : <{NS}> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    :if-a a :Interface ; :onDevice "Router-A" ; :interfaceName "ge0" .
    :if-b a :Interface ; :onDevice "Router-B" ; :interfaceName "ge1" .
    :if-c a :Interface ; :onDevice "Router-C" ; :interfaceName "ge2" .
    :if-d a :Interface ; :onDevice "Router-D" ; :interfaceName "ge3" .
    :if-lone a :Interface ; :onDevice "Router-E" ; :interfaceName "ge4" .
    :link-ab rdfs:label "A to B" . :link-ac rdfs:label "A to C" .
    """, format="turtle")
    ab = f.add_link_fluent(g, f"{NS}link-ab", f"{NS}if-a", f"{NS}if-b", T0, T0, T1)
    ac = f.add_link_fluent(g, f"{NS}link-ac", f"{NS}if-a", f"{NS}if-c", T1, T1)
    f.supersede(g, str(ac), T2)
    f.add_link_fluent(g, f"{NS}link-ac", f"{NS}if-a", f"{NS}if-d", T1, T2, kind="fluent-reissue")
    return GraphFuseki(g)


FNOW = datetime(2026, 11, 5, 10, 0, 0, tzinfo=timezone.utc)  # T1 + 2 days


def graph_neighbours(fuseki, device="Router-A", interface="ge0", **kw):
    with patch("broker_engine.operations.requests.post", fuseki.post):
        return ops.interface_neighbours(device, interface, now=FNOW, **kw)


def _devices(out):
    return [n["device"] for n in out["neighbours"]]


def test_neighbours_at_a_past_time_lists_the_links_valid_then():
    fuseki = _topology()
    assert _devices(graph_neighbours(fuseki, as_of="60h")) == ["Router-B"]  # between T0 and T1
    assert _devices(graph_neighbours(fuseki, as_of="24h")) == ["Router-D"]  # after T1, corrected
    assert _devices(graph_neighbours(fuseki)) == ["Router-D"]
    assert _devices(graph_neighbours(fuseki, as_of="60h", known_at="1h")) == ["Router-B"]


def test_neighbours_before_any_link_is_empty_not_missing():
    out = graph_neighbours(_topology(), as_of="5d")
    assert out["neighbours"] == [] and out["quality"]["missing"] is False
    out = graph_neighbours(_topology(), "Router-E", "ge4")
    assert out["neighbours"] == [] and out["quality"]["missing"] is False


def test_neighbours_unknown_interface_in_the_graph_is_missing():
    out = graph_neighbours(_topology(), "Router-Z", "ge9", as_of="60h")
    assert out["neighbours"] == [] and out["quality"]["missing"] is True


def test_neighbours_what_was_known_before_a_correction_differs_from_after_it():
    fuseki = _topology()
    # the same valid time (36h ago, after T1); the correction was recorded at T2, 24h ago
    before = graph_neighbours(fuseki, as_of="36h", known_at="25h")
    after = graph_neighbours(fuseki, as_of="36h", known_at="23h")
    assert _devices(before) == ["Router-C"] and _devices(after) == ["Router-D"]
    assert _devices(graph_neighbours(fuseki, as_of="36h")) == ["Router-D"]
    # before the broker recorded the A-C link (T1 is 48h ago) nothing is known about it
    assert graph_neighbours(fuseki, as_of="36h", known_at="49h")["neighbours"] == []


def test_neighbours_result_gains_valid_at_and_known_at_only_when_asked():
    fuseki = _topology()
    plain = graph_neighbours(fuseki)
    assert "valid_at" not in plain and "known_at" not in plain
    asked = graph_neighbours(fuseki, as_of="60h", known_at="31h")
    assert asked["valid_at"] == "2026-11-02T22:00:00.000Z"
    assert asked["known_at"] == "2026-11-04T03:00:00.000Z"
    only_as_of = graph_neighbours(fuseki, as_of="60h")
    assert only_as_of["valid_at"] == "2026-11-02T22:00:00.000Z" and only_as_of["known_at"] is None
    only_known = graph_neighbours(fuseki, known_at="31h")
    assert only_known["valid_at"] == "2026-11-05T10:00:00.000Z"


def test_neighbours_query_carries_the_formatted_instants_and_no_caller_offset_text():
    fuseki = _topology()
    graph_neighbours(fuseki, as_of="60h", known_at="31h")
    sent = fuseki.posts[-1][1]["data"]["query"]
    assert '"2026-11-02T22:00:00.000Z"^^xsd:dateTime' in sent
    assert '"2026-11-04T03:00:00.000Z"^^xsd:dateTime' in sent
    assert "60h" not in sent and "31h" not in sent
    graph_neighbours(fuseki)
    assert "FILTER(!BOUND(?sa))" in fuseki.posts[-1][1]["data"]["query"]


@pytest.mark.parametrize("kw", [
    {"as_of": "PT1H"}, {"as_of": "10"}, {"as_of": "5m } UNION { ?s ?p ?o"},
    {"as_of": "999d"}, {"known_at": "PT1H"}, {"known_at": "999d"}, {"known_at": 5},
])
def test_neighbours_rejects_bad_offsets_before_any_request(kw):
    with patch("broker_engine.operations.requests.post") as post:
        with pytest.raises(ClientInputError):
            ops.interface_neighbours("r1", "e0", now=FNOW, **kw)
    post.assert_not_called()


# ---------- wiring ----------
def test_agent_tools_are_registered_validated_and_described():
    import json
    from agent_harness import tools
    names = {t["name"] for t in tools.TOOLS}
    new = {"query_metric_history", "query_interfaces_with_nonzero", "query_interface_neighbours"}
    assert new <= names and {t["function"]["name"] for t in tools.TOOLS_OPENAI} == names
    for n in new:
        assert n in tools.SYSTEM_PROMPT
        assert hasattr(tools, n)
    assert "no data" in tools.SYSTEM_PROMPT and "intent" in tools.SYSTEM_PROMPT
    # allow-list: undeclared arguments (e.g. an endpoint override) are refused, nothing runs
    with patch("agent_harness.tools.operations.metric_history") as m:
        content, err = tools.execute_tool("query_metric_history", {
            "device": "r1", "interface": "e0", "metric_type": "m", "time_window": "5m",
            "query_endpoint": "http://evil"})
    m.assert_not_called()
    assert err and "query_endpoint" in json.loads(content)["error"]
    # bad input comes back as a tool error the model can read, not a crash
    content, err = tools.execute_tool("query_interfaces_with_nonzero", {
        "device": "r1", "metric_type": "input_drops", "time_window": "PT1H"})
    assert err and "time_window" in json.loads(content)["error"]


def test_nonzero_top_level_stale_only_when_every_examined_interface_is_stale():
    all_old = dict(SERIES, tlast_over_time=[(lab(i), T - 9000) for i in ("e0", "e1", "e2", "e3")])
    assert nz(all_old)[0]["quality"]["stale"] is True
    assert nz(SERIES)[0]["quality"]["stale"] is False
    some_old = dict(SERIES, tlast_over_time=[(lab("e0"), T - 9000)] +
                    [(lab(i), T - 10) for i in ("e1", "e2", "e3")])
    assert nz(some_old)[0]["quality"]["stale"] is False  # per-interface flags carry the detail
