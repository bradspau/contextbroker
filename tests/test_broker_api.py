import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from broker_engine import main
from broker_engine.pruner import prune
from data_contracts.envelope import build_observed

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
INTENT = {
    "state": "intended", "source": "fuseki", "metric": "latency_ms",
    "operator": "<", "target_value": 50.0,
    "interfaces": [
        {"device": "r1", "interface_name": "e0"},
        {"device": "r2", "interface_name": "e1"},
    ],
}
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


def test_assess_happy_path():
    with patch("broker_engine.main.resolve_intent", return_value=INTENT), \
         patch("broker_engine.main.join_observed_metric", side_effect=fake_join):
        r = client.post("/context/assess", json={"intent_id": "I1", "time_window": "5m"})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "success"
    assert len(body["per_interface"]) == 2
    assert len(body["context"]) == 2
    assert body["dropped_count"] == 0


def test_assess_verdict_independent_of_pruning():
    def join(device, interface, metric_type, time_window, **kw):
        if device == "r1":
            return build_observed("r1:e0", 99.0, "vm", NOW, NOW)
        return build_observed("r2:e1", 10.0, "vm", NOW, NOW)
    with patch("broker_engine.main.resolve_intent", return_value=INTENT), \
         patch("broker_engine.main.join_observed_metric", side_effect=join):
        r = client.post("/context/assess", json={"intent_id": "I1", "time_window": "5m"})
    assert r.json()["verdict"] == "failure"
    # direct call with tiny max_entities: floor is the interface count
    with patch("broker_engine.main.resolve_intent", return_value=INTENT), \
         patch("broker_engine.main.join_observed_metric", side_effect=join):
        out = main.assess("I1", "5m", max_entities=0)
    assert out["verdict"] == "failure"
    assert len(out["context"]) == 2 and out["dropped_count"] == 0


def test_assess_missing_telemetry_200():
    def join(device, interface, *a, **kw):
        return build_observed(f"{device}:{interface}", None, "vm", None, NOW)
    with patch("broker_engine.main.resolve_intent", return_value=INTENT), \
         patch("broker_engine.main.join_observed_metric", side_effect=join):
        r = client.post("/context/assess", json={"intent_id": "I1", "time_window": "5m"})
    assert r.status_code == 200
    assert r.json()["verdict"] == "inconclusive"
    assert r.json()["context"][0]["quality"]["missing"] is True


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


def test_intent_query_endpoint_rejected():
    with patch("broker_engine.main.resolve_intent", return_value=INTENT) as m:
        r = client.get("/context/intent/I1?query_endpoint=http://evil")
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
    r = client.get("/context/intent/bad%20id")
    assert r.status_code == 400


def test_unknown_intent_404():
    with patch("broker_engine.main.resolve_intent", side_effect=ValueError("No such intent: 'X'")):
        assert client.get("/context/intent/X").status_code == 404
        r = client.post("/context/assess", json={"intent_id": "X", "time_window": "5m"})
    assert r.status_code == 404


def test_intent_envelope_shape():
    with patch("broker_engine.main.resolve_intent", return_value=INTENT):
        r = client.get("/context/intent/I1")
    assert r.status_code == 200
    assert r.json()["metric"] == "latency_ms"


def test_mcp_tool_names():
    server = main.build_mcp_server()
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {"query_intent_state_requirements", "query_tsdb_metrics", "assess_intent"}


def test_mcp_assess_tool_call():
    server = main.build_mcp_server()
    with patch("broker_engine.main.resolve_intent", return_value=INTENT), \
         patch("broker_engine.main.join_observed_metric", side_effect=fake_join):
        res = asyncio.run(server.call_tool("assess_intent", {"intent_id": "I1", "time_window": "5m"}))
    import json
    assert json.loads(res.content[0].text)["verdict"] == "success"
