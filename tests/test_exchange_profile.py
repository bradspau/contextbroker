"""The JSON-LD exchange profile (XCH-01): the native response plus an inline static context.

The native response is the source; the profile only maps it. The tests prove the document is JSON-LD
a standard processor reads (rdflib, no network) and that the mapping converts back exactly.
"""
import copy
import json
import socket

import pytest
import rdflib
from rdflib import RDF, Literal, Namespace, URIRef

from data_contracts import exchange_profile as ep
from data_contracts.context_contract import ContextResponse
from tests import exchange_helpers as h

CB = Namespace(ep.NAMESPACE)


def _strip_keywords(value):
    """Independent of the module under test: drop every @type and @id key anywhere."""
    if isinstance(value, dict):
        return {k: _strip_keywords(v) for k, v in value.items() if k not in ("@type", "@id")}
    if isinstance(value, list):
        return [_strip_keywords(v) for v in value]
    return value


def _parse(doc) -> rdflib.Graph:
    graph = rdflib.Graph()
    graph.parse(data=json.dumps(doc), format="json-ld")
    return graph


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the exchange profile must never need the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_the_module_constants():
    assert ep.PROFILE_MEDIA_TYPE == "application/ld+json"
    assert isinstance(ep.CONTEXT, dict)
    assert ep.CONTEXT["@version"] == 1.1
    assert ep.CONTEXT["@base"] == ep.BASE_IRI
    assert ep.CONTEXT["cb"] == ep.NAMESPACE


def test_the_document_is_the_native_json_plus_a_context_and_injected_keys():
    response = h.topology_response()
    doc = ep.to_jsonld(response)
    assert next(iter(doc)) == "@context"
    assert doc["@context"] is ep.CONTEXT and isinstance(doc["@context"], dict)
    assert doc["@type"] == "cb:ContextResponse"
    assert "@id" not in doc
    body = {k: v for k, v in doc.items() if k != "@context"}
    assert _strip_keywords(body) == response.model_dump(mode="json", by_alias=True)


def test_nodes_and_the_situation_carry_their_ids_as_ids():
    doc = ep.to_jsonld(h.topology_response())
    assert doc["situation"]["@id"] == h.SITUATION_ID
    assert doc["situation"]["@type"] == "cb:Situation"
    assert [n["@id"] for n in doc["graph"]["nodes"]] == [n["id"] for n in doc["graph"]["nodes"]]
    assert all(n["@type"] == "cb:Node" for n in doc["graph"]["nodes"])
    assert all(e["@type"] == "cb:Edge" for e in doc["graph"]["edges"])


def test_the_converter_does_not_change_the_response_it_is_given():
    response = h.topology_response()
    before = copy.deepcopy(response)
    ep.to_jsonld(response)
    assert response == before


def test_rdflib_reads_it_as_json_ld_and_edges_link_their_nodes():
    graph = _parse(ep.to_jsonld(h.topology_response()))
    node = URIRef(ep.BASE_IRI + h.IF_A)
    assert (node, RDF.type, CB.Node) in graph
    assert (URIRef(ep.BASE_IRI + h.SITUATION_ID), RDF.type, CB.Situation) in graph
    edges = [s for s in graph.subjects(CB["type"], Literal("connected_to"))]
    assert len(edges) == 1
    start = graph.value(edges[0], CB["from"])
    end = graph.value(edges[0], CB["to"])
    assert start == node and isinstance(start, URIRef)
    assert end == URIRef(ep.BASE_IRI + h.IF_B) and isinstance(end, URIRef)


def test_it_converts_back_to_the_identical_native_answer():
    response = h.topology_response()
    assert ep.from_jsonld(ep.to_jsonld(response)) == response


def test_it_survives_a_trip_through_json_text():
    response = h.topology_response()
    text = json.dumps(ep.to_jsonld(response))
    assert ep.from_jsonld(json.loads(text)) == response


def test_from_jsonld_does_not_change_the_document_it_is_given():
    doc = ep.to_jsonld(h.topology_response())
    before = copy.deepcopy(doc)
    ep.from_jsonld(doc)
    assert doc == before


@pytest.mark.parametrize("context", ["https://example.org/other", {"@vocab": "https://example.org/"}, None, {}, []])
def test_a_document_with_any_other_context_is_refused(context):
    doc = ep.to_jsonld(h.topology_response())
    doc["@context"] = context
    with pytest.raises(ValueError):
        ep.from_jsonld(doc)


def test_a_document_without_a_context_is_refused():
    doc = ep.to_jsonld(h.topology_response())
    del doc["@context"]
    with pytest.raises(ValueError):
        ep.from_jsonld(doc)


@pytest.mark.parametrize("doc", [None, [], "text", 3])
def test_a_document_that_is_not_an_object_is_refused(doc):
    with pytest.raises(ValueError):
        ep.from_jsonld(doc)


def test_a_stray_key_is_an_error_and_not_silently_dropped():
    doc = ep.to_jsonld(h.topology_response())
    doc["graph"]["extra"] = 1
    with pytest.raises(ValueError):
        ep.from_jsonld(doc)


def test_a_wrong_injected_type_is_refused():
    doc = ep.to_jsonld(h.topology_response())
    doc["graph"]["nodes"][0]["@type"] = "cb:Edge"
    with pytest.raises(ValueError):
        ep.from_jsonld(doc)


def test_the_profile_module_is_pure():
    import ast
    import sys
    from pathlib import Path

    tree = ast.parse(Path(ep.__file__).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots.add(node.module.split(".")[0])
    assert roots - set(sys.stdlib_module_names) == {"data_contracts"}


def test_the_response_model_is_the_type_it_validates():
    assert isinstance(ep.from_jsonld(ep.to_jsonld(h.topology_response())), ContextResponse)
