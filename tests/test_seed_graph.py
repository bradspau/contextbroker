import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import rdflib

import seed_graph
from ingestion.load_ontologies import load_ontologies, validate_shapes

ONTOLOGY_PATH = Path(__file__).resolve().parent.parent / "ontologies" / "tbox" / "topology.ttl"
NS = rdflib.Namespace("http://example.org/network-ontology#")


@pytest.fixture(autouse=True)
def _fuseki_credential(monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "testpassword0")


DEMO_PATH = Path(__file__).resolve().parent.parent / "ontologies" / "abox" / "topology-demo.ttl"
SHACL_DIR = Path(__file__).resolve().parent.parent / "ontologies" / "shacl"
XSD = rdflib.XSD
INSTANCE_CLASSES = ("Interface", "TopologicalLink", "ServiceBinding", "GoalFluent", "PlacementFluent", "LinkFluent")


def test_topology_tbox_holds_schema_only():
    graph = rdflib.Graph()
    graph.parse(ONTOLOGY_PATH, format="turtle")

    for cls in INSTANCE_CLASSES:
        assert list(graph.subjects(rdflib.RDF.type, NS[cls])) == [], cls
        # the class is still declared
        assert (NS[cls], rdflib.RDF.type, rdflib.OWL.Class) in graph
    for prop in ("hasInterface", "ofLink", "onDevice", "interfaceName", "validStart", "recordedAt"):
        assert any(graph.triples((NS[prop], rdflib.RDF.type, None))), prop


def test_demo_topology_abox_defines_interfaces_and_a_link_fluent():
    graph = rdflib.Graph()
    graph.parse(DEMO_PATH, format="turtle")

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
    assert (link, rdflib.RDFS.label, rdflib.Literal("Router-A <-> Router-B core link")) in graph
    assert list(graph.objects(link, NS.hasInterface)) == []

    fluent = NS["link-router-a-router-b-fluent-demo"]
    assert (fluent, rdflib.RDF.type, NS.LinkFluent) in graph
    assert (fluent, NS.ofLink, link) in graph
    assert set(graph.objects(fluent, NS.hasInterface)) == {NS["if-router-a-ge000"], NS["if-router-b-ge001"]}
    stamp = rdflib.Literal("2026-11-05T00:00:00Z", datatype=XSD.dateTime)
    assert (fluent, NS.validStart, stamp) in graph
    assert (fluent, NS.recordedAt, stamp) in graph
    assert list(graph.objects(fluent, NS.validEnd)) == []
    assert list(graph.objects(fluent, NS.supersededAt)) == []


def test_demo_topology_abox_conforms_to_every_shapes_file():
    for shapes in sorted(SHACL_DIR.glob("*.ttl")):
        ok, report = validate_shapes(DEMO_PATH, shapes)
        assert ok, report


def test_a_link_node_that_still_carries_the_interface_pair_does_not_conform(tmp_path):
    old = tmp_path / "old-link.ttl"
    old.write_text(
        "@prefix : <http://example.org/network-ontology#> .\n"
        ":l a :TopologicalLink ; :hasInterface :a , :b .\n"
    )
    ok, report = validate_shapes(old, SHACL_DIR / "topology.shacl.ttl")
    assert ok is False
    assert "hasInterface" in report


def test_load_ontologies_validates_and_posts_the_tbox_and_the_demo_file(tmp_path):
    # Tracked file only: the operator's binding files in ontologies/abox are not under test.
    abox_dir = tmp_path / "abox"
    abox_dir.mkdir()
    shutil.copy(DEMO_PATH, abox_dir)
    resp = Mock()
    resp.raise_for_status = Mock()
    with patch("ingestion.load_ontologies.requests.post", return_value=resp) as post:
        load_ontologies(ONTOLOGY_PATH.parent, SHACL_DIR, "http://x/data", abox_dir=abox_dir)
    sent = [call.kwargs["data"] for call in post.call_args_list]
    assert sent[0] == ONTOLOGY_PATH.read_bytes()
    assert DEMO_PATH.read_bytes() in sent
    assert len(sent) == 2


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_tracks_only_the_demo_abox_file():
    root = Path(__file__).resolve().parent.parent
    demo = subprocess.run(["git", "check-ignore", "-q", "ontologies/abox/topology-demo.ttl"], cwd=root)
    other = subprocess.run(["git", "check-ignore", "-q", "ontologies/abox/some-intent.ttl"], cwd=root)
    assert demo.returncode == 1
    assert other.returncode == 0


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


SEED_SCRIPT = Path(__file__).resolve().parent.parent / "seed_graph.py"


def test_seed_graph_exits_naming_the_variable_when_unset(monkeypatch):
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(sys, "argv", ["seed_graph.py"])
    with patch("requests.post") as post:
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(SEED_SCRIPT), run_name="__main__")
    assert "FUSEKI_ADMIN_PASSWORD" in str(exc.value)
    post.assert_not_called()


def test_seed_graph_posts_with_the_environment_credential(monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "seed0value")
    monkeypatch.setattr(sys, "argv", ["seed_graph.py"])
    mock_response = Mock()
    mock_response.status_code = 204
    mock_response.raise_for_status = Mock()
    with patch("requests.post", return_value=mock_response) as post:
        runpy.run_path(str(SEED_SCRIPT), run_name="__main__")
    post.assert_called_once()
    assert post.call_args.kwargs["auth"] == ("admin", "seed0value")
