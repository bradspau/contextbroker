"""The JSON-LD exchange profile (XCH-01): the native response plus an inline static context.

The native response is the source; the profile only maps it. The tests prove the document is JSON-LD
a standard processor reads (rdflib, no network) and that the mapping converts back exactly.
"""
import copy
import json
import socket
from pathlib import Path
from typing import get_args

import pytest
from pydantic import BaseModel
import rdflib
from rdflib import RDF, Literal, Namespace, URIRef

from data_contracts import exchange_profile as ep
from data_contracts.context_contract import ContextResponse
from tests import exchange_helpers as h
from tests.test_knowledge_bundles import FORBIDDEN_MARKERS

CB = Namespace(ep.NAMESPACE)
FIXTURES = Path(__file__).parent / "fixtures" / "exchange"
# Counted once from the reviewed fixture. A key that loses its term lowers this number.
FIXTURE_TRIPLES = 291


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


# ---- the whole response: all five classes, a verdict, a promoted conflict and a hypothesis ----

def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_a_full_response_round_trips_exactly():
    response = h.full_response()
    assert ep.from_jsonld(ep.to_jsonld(response)) == response
    assert ep.from_jsonld(json.loads(json.dumps(ep.to_jsonld(response)))) == response


def test_both_directions_equal_the_known_answer_fixtures():
    response = h.full_response()
    assert _fixture("response.jsonld") == ep.to_jsonld(response)
    assert _fixture("response-native.json") == response.model_dump(mode="json", by_alias=True)
    assert ep.from_jsonld(_fixture("response.jsonld")) == ContextResponse.model_validate(
        _fixture("response-native.json"))


def test_the_fixture_files_are_exactly_what_the_helper_writes(tmp_path):
    h.write_fixtures(tmp_path)
    for name in ("response-native.json", "response.jsonld"):
        assert (tmp_path / name).read_bytes() == (FIXTURES / name).read_bytes()
        assert (tmp_path / name).read_bytes().endswith(b"}\n")


def test_the_fixtures_hold_no_licensed_marker():
    for name in ("response-native.json", "response.jsonld"):
        text = (FIXTURES / name).read_text(encoding="utf-8").lower()
        for marker in FORBIDDEN_MARKERS:
            assert marker.lower() not in text, (name, marker)


def _wire_keys_without_a_term(context):
    seen, pending, missing = set(), [ContextResponse], []
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        for name, field in model.model_fields.items():
            key = field.alias or name
            if key not in context:
                missing.append(f"{model.__name__}.{key}")
            pending.extend(_models_in(field.annotation))
    assert {m.__name__ for m in seen} >= {
        "ContextResponse", "Situation", "Scope", "Eligibility", "FluentValue", "EnvelopeOut", "ContextGraph",
        "GraphEdge", "HypothesisEdge", "SupportRef", "EventsResult", "PromotedConflictOut", "KnowledgeResult",
        "KnowledgeRef", "ActionRef", "MatchedScope", "Provenance"}
    return sorted(set(missing))


def _models_in(annotation):
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in get_args(annotation):
        yield from _models_in(arg)


def test_every_wire_key_of_every_response_model_has_a_term():
    assert _wire_keys_without_a_term(ep.CONTEXT) == []


def test_the_term_walk_names_a_key_that_loses_its_term():
    for key in ("class", "from", "promoted_conflicts", "support"):
        reduced = {k: v for k, v in ep.CONTEXT.items() if k != key}
        missing = _wire_keys_without_a_term(reduced)
        assert missing and all(entry.endswith("." + key) for entry in missing)


def test_every_type_path_is_used_by_the_full_response():
    found = set()

    def collect(value):
        if isinstance(value, dict):
            if "@type" in value and not isinstance(value["@type"], dict):
                found.add(value["@type"])
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect({k: v for k, v in ep.to_jsonld(h.full_response()).items() if k not in ("@context", "verdict")})
    assert {type_ for _, type_, _ in ep.TYPE_PATHS} == found


def _expected_triples(node):
    """Triples a lossless expansion must yield, counted from the document without a JSON-LD processor:
    one per type, one per non-null scalar or list item, one per nested object plus its own, and one per
    @json literal."""
    count = 1 if "@type" in node else 0
    for key, value in node.items():
        if key.startswith("@") or value is None:
            continue
        term = ep.CONTEXT[key]
        if isinstance(term, dict) and term.get("@type") == "@json":
            count += 1
        elif isinstance(value, dict):
            count += 1 + _expected_triples(value)
        elif isinstance(value, list):
            count += sum(1 + _expected_triples(i) if isinstance(i, dict) else 1 for i in value)
        else:
            count += 1
    return count


def test_the_fixture_is_real_json_ld_with_the_pinned_triple_count():
    document = _fixture("response.jsonld")
    graph = _parse(document)
    assert len(graph) == FIXTURE_TRIPLES
    assert _expected_triples(document) == FIXTURE_TRIPLES  # nothing was dropped by the processor


def test_a_hypothesis_and_a_promoted_conflict_are_typed_nodes_with_their_labels():
    response = h.full_response()
    graph = _parse(ep.to_jsonld(response))
    hypothesis = URIRef(ep.BASE_IRI + response.graph.hypotheses[0].promoted_id)
    conflict = URIRef(ep.BASE_IRI + response.events.promoted_conflicts[0].promoted_id)
    assert (hypothesis, RDF.type, CB.Hypothesis) in graph
    assert (hypothesis, CB.label, Literal("hypothesis")) in graph
    assert (hypothesis, CB.state, Literal("inferred")) in graph
    assert (conflict, RDF.type, CB.PromotedConflict) in graph
    assert graph.value(hypothesis, CB["from"]) == URIRef(ep.BASE_IRI + h.HYPOTHESIS_ENDS[0])
    assert graph.value(hypothesis, CB["to"]) == URIRef(ep.BASE_IRI + h.HYPOTHESIS_ENDS[1])


def test_a_hypothesis_is_not_among_the_configured_edges_in_the_document():
    doc = ep.to_jsonld(h.full_response())
    assert all(edge["type"] in ("has_interface", "connected_to") for edge in doc["graph"]["edges"])
    assert [e["label"] for e in doc["graph"]["hypotheses"]] == ["hypothesis"]


def test_the_verdict_is_one_json_literal():
    graph = _parse(ep.to_jsonld(h.full_response()))
    verdicts = [o for _, _, o in graph.triples((None, CB.verdict, None))]
    assert len(verdicts) == 1
    assert isinstance(verdicts[0], Literal) and verdicts[0].datatype == RDF.JSON


def test_a_verdict_with_keyword_keys_is_data_and_never_read_as_json_ld():
    verdict = {"verdict": "inconclusive", "@context": "https://attacker.example/ctx", "@id": "x", "reasons": []}
    response = h.full_response().model_copy(update={"verdict": verdict})
    doc = ep.to_jsonld(response)
    assert doc["verdict"] == verdict and "@type" not in doc["verdict"]
    assert ep.from_jsonld(json.loads(json.dumps(doc))) == response
    graph = _parse(doc)
    verdicts = [o for _, _, o in graph.triples((None, CB.verdict, None))]
    assert len(verdicts) == 1 and verdicts[0].datatype == RDF.JSON
    for triple in graph:
        for term in triple:
            if isinstance(term, Literal) and term.datatype == RDF.JSON:
                continue  # the verdict's own text is data inside one literal
            assert "attacker.example" not in str(term)
    assert (URIRef(ep.BASE_IRI + "x"), None, None) not in graph


def test_the_classes_maps_are_json_literals_too():
    graph = _parse(ep.to_jsonld(h.full_response()))
    literals = [o for _, _, o in graph.triples((None, CB.classes, None))]
    assert len(literals) == 2 and all(o.datatype == RDF.JSON for o in literals)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_the_context_is_inline_and_names_no_remote_context():
    assert isinstance(ep.CONTEXT, dict)
    remote = {s for s in _strings(ep.CONTEXT) if s.startswith("http")}
    assert remote == {ep.BASE_IRI, ep.NAMESPACE, "http://www.w3.org/ns/prov#",
                      "http://www.w3.org/2001/XMLSchema#", "http://purl.org/dc/terms/"}
    assert "@vocab" not in ep.CONTEXT and "@import" not in ep.CONTEXT
