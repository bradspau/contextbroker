import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import anthropic
import httpx2
import pytest
import rdflib

import broker
import telemetry_simulator


ONTOLOGY_PATH = Path(__file__).resolve().parent.parent / "ontology" / "topology.ttl"


SPARQL_RESPONSE = {
    "results": {
        "bindings": [
            {
                "metric": {"value": "input_drops"},
                "operator": {"value": "=="},
                "targetValue": {"value": "0.0"},
                "device": {"value": "Router-A"},
                "interfaceName": {"value": "GigabitEthernet0/0/0"},
            },
            {
                "metric": {"value": "input_drops"},
                "operator": {"value": "=="},
                "targetValue": {"value": "0.0"},
                "device": {"value": "Router-B"},
                "interfaceName": {"value": "GigabitEthernet0/0/1"},
            },
        ]
    }
}

VM_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [{"metric": {}, "value": [1700000000, "3"]}],
    },
}

VM_EMPTY_RESPONSE = {"status": "success", "data": {"resultType": "vector", "result": []}}


def test_query_intent_state_requirements_parses_sparql_json():
    mock_response = Mock()
    mock_response.json.return_value = SPARQL_RESPONSE
    mock_response.raise_for_status = Mock()

    with patch("broker.requests.post", return_value=mock_response) as mock_post:
        result = broker.query_intent_state_requirements("intent-core-health-01")

    assert result["metric"] == "input_drops"
    assert result["operator"] == "=="
    assert result["target_value"] == 0.0
    assert result["interfaces"] == [
        {"device": "Router-A", "interface_name": "GigabitEthernet0/0/0"},
        {"device": "Router-B", "interface_name": "GigabitEthernet0/0/1"},
    ]
    _, kwargs = mock_post.call_args
    assert "intent-core-health-01" in kwargs["data"]["query"]


def test_query_intent_state_requirements_rejects_unsafe_intent_id():
    with pytest.raises(ValueError):
        broker.query_intent_state_requirements("intent-1 . } DROP ALL ; #")


def test_query_tsdb_metrics_parses_vector_result():
    mock_response = Mock()
    mock_response.json.return_value = VM_RESPONSE
    mock_response.raise_for_status = Mock()

    with patch("broker.requests.get", return_value=mock_response) as mock_get:
        result = broker.query_tsdb_metrics(
            device="Router-A",
            interface="GigabitEthernet0/0/0",
            metric_type="input_drops",
            time_window="5m",
        )

    assert result == 3.0
    _, kwargs = mock_get.call_args
    query = kwargs["params"]["query"]
    assert "interface_metrics_input_drops" in query
    assert 'device="Router-A"' in query
    assert 'interface="GigabitEthernet0/0/0"' in query
    assert "[5m]" in query


def test_query_tsdb_metrics_returns_none_when_no_data():
    mock_response = Mock()
    mock_response.json.return_value = VM_EMPTY_RESPONSE
    mock_response.raise_for_status = Mock()

    with patch("broker.requests.get", return_value=mock_response):
        result = broker.query_tsdb_metrics(
            device="Router-A",
            interface="GigabitEthernet0/0/0",
            metric_type="input_drops",
            time_window="5m",
        )

    assert result is None


@pytest.mark.parametrize(
    "device,interface,metric_type,time_window",
    [
        ("Router-A; DROP", "GigabitEthernet0/0/0", "input_drops", "5m"),
        ("Router-A", "Gi0/0/0\"}", "input_drops", "5m"),
        ("Router-A", "GigabitEthernet0/0/0", "input_drops", "5m; rm -rf"),
    ],
)
def test_query_tsdb_metrics_rejects_unsafe_inputs(device, interface, metric_type, time_window):
    with pytest.raises(ValueError):
        broker.query_tsdb_metrics(
            device=device, interface=interface, metric_type=metric_type, time_window=time_window
        )


def test_measurement_name_constant_matches_telemetry_simulator():
    """Finding 5: broker.py must import the shared constant, not hardcode its own copy."""
    assert broker.MEASUREMENT_NAME == telemetry_simulator.MEASUREMENT_NAME == "interface_metrics"


def test_intent_sparql_matches_real_ontology_content():
    """Finding 6: run the actual SPARQL text broker builds against the real ontology
    file (no HTTP mocking) so a predicate typo would fail this test, not just the
    mocked unit tests above."""
    graph = rdflib.Graph()
    graph.parse(ONTOLOGY_PATH, format="turtle")

    sparql = broker._build_intent_sparql("intent-core-health-01")
    rows = list(graph.query(sparql))

    assert len(rows) == 2
    seen = {(str(row.device), str(row.interfaceName)) for row in rows}
    assert seen == {
        ("Router-A", "GigabitEthernet0/0/0"),
        ("Router-B", "GigabitEthernet0/0/1"),
    }
    for row in rows:
        assert str(row.metric) == "input_drops"
        assert str(row.operator) == "=="
        assert str(row.targetValue) == "0.0"
