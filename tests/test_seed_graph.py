from pathlib import Path
from unittest.mock import Mock, patch

import rdflib

import seed_graph

ONTOLOGY_PATH = Path(__file__).resolve().parent.parent / "ontology" / "topology.ttl"
NS = rdflib.Namespace("http://example.org/network-ontology#")


def test_topology_ttl_parses_and_defines_intent_core_health_01():
    graph = rdflib.Graph()
    graph.parse(ONTOLOGY_PATH, format="turtle")

    assert len(graph) > 0
    intent = NS["intent-core-health-01"]
    assert (intent, NS.hasTargetMetric, rdflib.Literal("input_drops")) in graph
    assert (intent, NS.hasOperator, rdflib.Literal("==")) in graph


def test_load_ttl_to_fuseki_posts_raw_turtle_bytes():
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.raise_for_status = Mock()

    with patch("seed_graph.requests.post", return_value=mock_response) as mock_post:
        response = seed_graph.load_ttl_to_fuseki(
            ONTOLOGY_PATH, "http://localhost:3030/network-dataset/data"
        )

    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs["headers"] == {"Content-Type": "text/turtle"}
    assert kwargs["data"] == ONTOLOGY_PATH.read_bytes()
    assert kwargs["auth"] == ("admin", "admin")
    assert response is mock_response


def test_load_ttl_to_fuseki_raises_on_http_error():
    mock_response = Mock()
    mock_response.raise_for_status.side_effect = Exception("500 Server Error")

    with patch("seed_graph.requests.post", return_value=mock_response):
        try:
            seed_graph.load_ttl_to_fuseki(
                ONTOLOGY_PATH, "http://localhost:3030/network-dataset/data"
            )
            assert False, "expected an exception"
        except Exception as exc:
            assert "500" in str(exc)
