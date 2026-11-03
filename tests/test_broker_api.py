import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from broker_engine import main
from broker_engine.pruner import prune
from data_contracts.envelope import build_observed

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
client = TestClient(main.app)


def healthy(i):
    return build_observed(f"d:i{i}", 1.0, "vm", NOW, NOW)


def missing(i):
    return build_observed(f"d:m{i}", None, "vm", None, NOW)


def stale(i):
    return build_observed(f"d:s{i}", 1.0, "vm", NOW - timedelta(hours=1), NOW)


def fake_join(device, interface, metric_type, time_window, **kw):
    return build_observed(f"{device}:{interface}", 10.0, "vm", NOW, NOW)


def test_prune_reports_dropped_count():
    envs = [healthy(i) for i in range(5)]
    res = prune(envs, 2)
    assert len(res.kept) == 2 and res.dropped_count == 3
    assert res.reasons == ["dropped 3 healthy envelopes"]


def test_prune_never_drops_missing_or_stale_first():
    envs = [healthy(0), healthy(1), missing(0), stale(0), healthy(2)]
    res = prune(envs, 2)
    assert {e.entity_id for e in res.kept} == {"d:m0", "d:s0"}
    assert res.dropped_count == 3


def test_prune_no_drop_when_within_limit():
    envs = [healthy(0), missing(0)]
    res = prune(envs, 5)
    assert res.kept == envs and res.dropped_count == 0 and res.reasons == []


def test_prune_zero_max():
    res = prune([healthy(0)], 0)
    assert res.kept == [] and res.dropped_count == 1


def test_metric_missing_telemetry_200():
    env = build_observed("r1:e0", None, "vm", None, NOW)
    with patch("broker_engine.main.join_observed_metric", return_value=env):
        r = client.get("/context/metric", params={
            "device": "r1", "interface": "e0", "metric_type": "latency_ms", "time_window": "5m"})
    assert r.status_code == 200
    assert r.json()["quality"]["missing"] is True


def test_metric_query_endpoint_rejected():
    with patch("broker_engine.main.join_observed_metric", side_effect=fake_join) as m:
        r = client.get("/context/metric", params={
            "device": "r1", "interface": "e0", "metric_type": "latency_ms",
            "time_window": "5m", "query_endpoint": "http://evil"})
    assert r.status_code == 422
    m.assert_not_called()


def test_assess_extra_body_field_rejected():
    r = client.post("/context/assess", json={
        "intent_id": "I1", "time_window": "5m", "query_endpoint": "http://evil"})
    assert r.status_code == 422


def test_unsafe_token_400():
    r = client.get("/context/metric", params={
        "device": "r1;drop", "interface": "e0", "metric_type": "latency_ms", "time_window": "5m"})
    assert r.status_code == 400
    r = client.get("/context/neighbours", params={"device": "r1", "interface": "e 0"})
    assert r.status_code == 400


def test_mcp_tool_names():
    server = main.build_mcp_server()
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {"query_tsdb_metrics", "assess_intent",
                     "query_metric_history", "query_interfaces_with_nonzero",
                     "query_interface_neighbours"}


def _metric_params():
    return {"device": "r1", "interface": "e0", "metric_type": "latency_ms", "time_window": "5m"}


def _neighbour_params():
    return {"device": "r1", "interface": "e0"}


def test_other_valueerror_is_not_400():
    c = TestClient(main.app, raise_server_exceptions=False)
    with patch("broker_engine.main.operations.interface_neighbours", side_effect=ValueError("secret upstream")):
        r = c.get("/context/neighbours", params=_neighbour_params())
    assert r.status_code == 500
    assert "secret" not in r.text


def test_upstream_non_json_is_5xx_not_400():
    import requests as rq
    resp = type("R", (), {"raise_for_status": lambda self: None,
                          "json": lambda self: (_ for _ in ()).throw(rq.exceptions.JSONDecodeError("bad body SECRET", "", 0))})()
    c = TestClient(main.app, raise_server_exceptions=False)
    with patch("broker_engine.tsdb_joiner.requests.get", return_value=resp):
        r = c.get("/context/metric", params=_metric_params())
    assert r.status_code >= 500 and "SECRET" not in r.text


@pytest.mark.parametrize("target,url,params", [
    ("broker_engine.operations.requests.post", "/context/neighbours", _neighbour_params()),
    ("broker_engine.tsdb_joiner.requests.get", "/context/metric", _metric_params()),
])
def test_connection_error_is_502(target, url, params):
    import requests as rq
    with patch(target, side_effect=rq.ConnectionError("http://10.0.0.1:3030 refused")):
        r = client.get(url, params=params)
    assert r.status_code == 502
    assert "10.0.0.1" not in r.text


def _vector(value, ts=None):
    """A VictoriaMetrics instant-query response carrying one series."""
    from unittest.mock import MagicMock
    r = MagicMock()
    r.json.return_value = {"data": {"result": [{"value": [1, value]}]}}
    return r


@pytest.mark.parametrize("raw", ["NaN", "+Inf", "-Inf"])
def test_metric_non_finite_sample_is_missing_at_the_endpoint(raw):
    with patch("broker_engine.tsdb_joiner.requests.get", return_value=_vector(raw)):
        r = client.get("/context/metric", params=_metric_params())
    assert r.status_code == 200
    body = r.json()
    assert body["value"] is None
    assert body["quality"]["missing"] is True
    assert body["quality"]["stale"] is False


def test_metric_unparsable_sample_is_missing():
    with patch("broker_engine.tsdb_joiner.requests.get", return_value=_vector("not-a-number")):
        r = client.get("/context/metric", params=_metric_params())
    assert r.status_code == 200
    body = r.json()
    assert body["value"] is None
    assert body["quality"]["missing"] is True
    assert "not-a-number" not in r.text


def test_metric_missing_sample_still_reports_multiple_series():
    from unittest.mock import MagicMock
    resp = MagicMock()
    resp.json.return_value = {"data": {"result": [{"value": [1, "NaN"]}, {"value": [1, "5"]}]}}
    with patch("broker_engine.tsdb_joiner.requests.get", return_value=resp):
        r = client.get("/context/metric", params=_metric_params())
    body = r.json()
    assert body["quality"]["missing"] is True
    assert body["quality"]["conflicts"] == ["multiple series match selector (2)"]


def test_timeout_passed_to_requests():
    from unittest.mock import MagicMock
    from broker_engine.validation import REQUEST_TIMEOUT_S
    r = MagicMock()
    r.json.return_value = {"data": {"result": [{"value": [1, "5"]}]}}
    with patch("broker_engine.tsdb_joiner.requests.get", return_value=r) as g:
        main.join_observed_metric("r1", "e0", "latency_ms", "5m")
    assert len(g.call_args_list) == 2
    assert all(c.kwargs["timeout"] == REQUEST_TIMEOUT_S for c in g.call_args_list)
    r2 = MagicMock()
    r2.json.return_value = {"results": {"bindings": [{"x": 1}]}}
    from broker_engine.binding_resolver import resolve_binding
    with patch("broker_engine.binding_resolver.requests.post", return_value=r2) as p:
        try:
            resolve_binding("I1")
        except Exception:
            pass
    assert p.call_args.kwargs["timeout"] == REQUEST_TIMEOUT_S


def test_mcp_metric_tool_returns_envelope():
    import json
    server = main.build_mcp_server()
    env = build_observed("r1:e0", 1.0, "vm", NOW - timedelta(hours=1), NOW)
    with patch("broker_engine.main.join_observed_metric", return_value=env):
        res = asyncio.run(server.call_tool("query_tsdb_metrics", _metric_params()))
    payload = json.loads(res.content[0].text)
    assert payload["quality"]["stale"] is True and payload["value"] == 1.0


# --- source (tmf921 only) ---

def test_default_source_is_tmf921():
    from broker_engine.assess import assess_intent
    ret = {"verdict": "success", "source": "tmf921"}
    with patch("broker_engine.assess.assess_via_tmf921", return_value=ret) as via:
        assert assess_intent("I1", "5m") == ret
        via.assert_called_once()
    # REST body without source
    with patch("broker_engine.assess.assess_via_tmf921", return_value=ret) as via:
        r = TestClient(main.app).post("/context/assess", json={"intent_id": "I1", "time_window": "5m"})
    assert r.status_code == 200 and r.json() == ret
    via.assert_called_once()
    # MCP tool without source
    import json
    server = main.build_mcp_server()
    with patch("broker_engine.assess.assess_via_tmf921", return_value=ret) as via:
        out = asyncio.run(server.call_tool("assess_intent", {"intent_id": "I1", "time_window": "5m"}))
    via.assert_called_once()
    assert json.loads(out.content[0].text) == ret
    assert main.AssessRequest(intent_id="I1", time_window="5m").source == "tmf921"


def test_tmf921_response_has_no_deprecated_key():
    with patch("broker_engine.assess.assess_via_tmf921", return_value={"verdict": "success"}):
        r = TestClient(main.app).post(
            "/context/assess", json={"intent_id": "I1", "time_window": "5m", "source": "tmf921"})
    assert "deprecated" not in r.json()


def test_assess_source_tmf921_routes_to_new_path():
    with patch("broker_engine.assess.assess_via_tmf921", return_value={"verdict": "success", "source": "tmf921"}) as via:
        r = TestClient(main.app).post(
            "/context/assess", json={"intent_id": "I1", "time_window": "5m", "source": "tmf921"})
    assert r.status_code == 200 and r.json()["source"] == "tmf921"
    assert via.call_args.args == ("I1", "5m")
    assert via.call_args.kwargs["join"] is main.join_observed_metric


def test_assess_unknown_source_422_and_python_clientinputerror():
    r = TestClient(main.app).post(
        "/context/assess", json={"intent_id": "I1", "time_window": "5m", "source": "bogus"})
    assert r.status_code == 422
    from broker_engine.assess import assess_intent
    from broker_engine.validation import ClientInputError
    with pytest.raises(ClientInputError):
        assess_intent("I1", "5m", source="bogus")


def test_assess_extra_field_still_422_with_source():
    r = TestClient(main.app).post(
        "/context/assess", json={"intent_id": "I1", "time_window": "5m", "source": "tmf921", "x": 1})
    assert r.status_code == 422


def test_assess_tmf921_client_input_error_is_400():
    from broker_engine.validation import ClientInputError
    with patch("broker_engine.assess.assess_via_tmf921", side_effect=ClientInputError("bad")):
        r = TestClient(main.app).post(
            "/context/assess", json={"intent_id": "I1", "time_window": "5m", "source": "tmf921"})
    assert r.status_code == 400



def test_mcp_assess_tool_has_no_source_argument():
    import json
    server = main.build_mcp_server()
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "assess_intent")
    assert "source" not in tool.input_schema["properties"]
    with patch("broker_engine.assess.assess_via_tmf921", return_value={"verdict": "success", "source": "tmf921"}) as via:
        res = asyncio.run(server.call_tool("assess_intent", {"intent_id": "I1", "time_window": "5m"}))
    via.assert_called_once()
    assert json.loads(res.content[0].text)["source"] == "tmf921"


def test_source_graph_is_refused_at_rest():
    with patch("broker_engine.assess.assess_via_tmf921") as via:
        r = TestClient(main.app).post(
            "/context/assess", json={"intent_id": "I1", "time_window": "5m", "source": "graph"})
    assert r.status_code == 422
    via.assert_not_called()


def test_intent_route_is_gone():
    assert client.get("/context/intent/I1").status_code == 404
    assert not [r.path for r in main.app.routes if r.path.startswith("/context/intent")]


# --- operations routes still answer (moved from tests/test_operations.py) ---

def test_rest_endpoints_pass_arguments_through_and_reject_extras():
    c = TestClient(main.app)
    with patch.object(main.operations, "metric_history", return_value={"ok": 1}) as h:
        r = c.get("/context/metric-history", params={
            "device": "r1", "interface": "e0", "metric_type": "input_drops",
            "time_window": "30m", "as_of": "10m"})
        assert r.status_code == 200
        assert h.call_args.args == ("r1", "e0", "input_drops", "30m", "10m")
        assert c.get("/context/metric-history", params={
            "device": "r1", "interface": "e0", "metric_type": "m", "time_window": "5m",
            "query_endpoint": "http://evil"}).status_code == 422
    with patch.object(main.operations, "interfaces_with_nonzero", return_value={"ok": 1}) as n:
        r = c.get("/context/interfaces-nonzero", params={
            "device": "r1", "metric_type": "input_drops", "time_window": "1h", "limit": 5})
        assert r.status_code == 200 and n.call_args.args == ("r1", "input_drops", "1h", None, 5)
    with patch.object(main.operations, "interface_neighbours", return_value={"ok": 1}) as nb:
        assert c.get("/context/neighbours", params={"device": "r1", "interface": "e0"}).status_code == 200
        assert nb.call_args.args == ("r1", "e0")


def test_rest_bad_input_is_a_400():
    c = TestClient(main.app)
    r = c.get("/context/metric-history", params={
        "device": "r1", "interface": "e0", "metric_type": "m", "time_window": "PT1H"})
    assert r.status_code == 400
    assert c.get("/context/neighbours", params={"device": 'a"b', "interface": "e0"}).status_code == 400


def test_rest_passes_as_of_and_mode_through():
    with patch.object(main, "assess", return_value={"verdict": "success"}) as a:
        c = TestClient(main.app)
        r = c.post("/context/assess", json={"intent_id": "i1", "time_window": "5m",
                                            "as_of": "10m", "mode": "within"})
        assert r.status_code == 200
        assert a.call_args.kwargs["as_of"] == "10m" and a.call_args.kwargs["mode"] == "within"
        assert c.post("/context/assess", json={"intent_id": "i1", "time_window": "5m",
                                               "mode": "bogus"}).status_code == 422
