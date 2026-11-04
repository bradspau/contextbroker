from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import rdflib

import seed_graph

ONTOLOGY_PATH = Path(__file__).resolve().parent.parent / "ontologies" / "tbox" / "topology.ttl"
NS = rdflib.Namespace("http://example.org/network-ontology#")


@pytest.fixture(autouse=True)
def _fuseki_credential(monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "testpassword0")


def test_topology_ttl_defines_the_demo_topology():
    graph = rdflib.Graph()
    graph.parse(ONTOLOGY_PATH, format="turtle")

    assert len(graph) > 0
    for iface, device, name in (
        ("if-router-a-ge000", "Router-A", "GigabitEthernet0/0/0"),
        ("if-router-b-ge001", "Router-B", "GigabitEthernet0/0/1"),
    ):
        node = NS[iface]
        assert (node, rdflib.RDF.type, NS.Interface) in graph
        assert (node, NS.onDevice, rdflib.Literal(device)) in graph
        assert (node, NS.interfaceName, rdflib.Literal(name)) in graph
    link = NS["link-router-a-router-b"]
    assert (link, rdflib.RDF.type, NS.TopologicalLink) in graph
    assert (link, NS.hasInterface, NS["if-router-a-ge000"]) in graph
    assert (link, NS.hasInterface, NS["if-router-b-ge001"]) in graph


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
    assert kwargs["auth"] == ("admin", "testpassword0")
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
