"""The JSON-LD exchange profile of the context response (XCH-01).

The native response (`ContextResponse`) is the source and this module only maps it. The mapped document
is the native JSON, by alias exactly as the route would serialise it, plus three things and nothing
else:

- an inline, static `@context` (the dict `CONTEXT`). A consumer never fetches a context: no string in it
  is a remote reference, and there is no route that serves one (D-53);
- an `@type`, and where the model has an identity an `@id`, injected at the fixed positions listed in
  `TYPE_PATHS`. That table is the only place an injected key is named, and `from_jsonld` removes
  exactly the keys `to_jsonld` added, by the same walker, so the two directions cannot disagree;
- nothing inside a value whose term is `@json` (the verdict, which is a free-form dict, and the
  `classes` maps). Those stay opaque JSON literals, so a key that starts with `@` in a verdict is
  data, never a JSON-LD keyword.

Every node, situation and event identifier keeps its native string. `@base` makes each of them one IRI
without rewriting the value, so edges link their `from` and `to` node IRIs. Array order is the native
order and carries no further meaning: ordered lists (events newest first, references in rank) carry
the order in explicit fields.

A key with no term is silently dropped by a JSON-LD processor, so every wire key has a term and a test
walks the response models to prove it. This module is pure: standard library and the contract models
only, with no FastAPI and no JSON-LD library.
"""
import copy
import re

from data_contracts.context_contract import ContextResponse

NAMESPACE = "https://example.org/ns/context-broker#"
BASE_IRI = "https://example.org/ns/context-broker/id/"
PROFILE_MEDIA_TYPE = "application/ld+json"

_NOT_A_PROFILE = "not a context profile document"

# Times are typed so a processor reads them as instants. observed_at is the PROV-O generation time of the
# evidence; retrieved_at has no exact PROV-O property, so it stays a broker term.
_TIME_TERMS = ("expires_at", "valid_at", "known_at", "retrieved_at", "valid_from", "valid_to", "recorded_at",
               "superseded_at", "event_time", "ingest_time", "start", "end", "promoted_at")
# Keys whose value names another node: this is what makes an edge a link between its two nodes.
_LINK_TERMS = ("from", "to", "node", "event_id")
# Keys that stay one opaque JSON literal. The verdict is free-form; `classes` is a map keyed by class
# name in the eligibility and a list in the situation, and neither is a natural RDF structure.
_JSON_TERMS = ("verdict", "classes")
_DUBLIN_CORE = {"identifier": "dcterms:identifier", "title": "dcterms:title"}
# Every other wire key is a plain broker term.
_PLAIN_TERMS = (
    "approved", "authority_cut", "candidates", "candidates_withheld", "cap", "conflicts", "configured",
    "content_hash", "depth", "device", "edges", "eligibility", "entities", "entity_id", "envelopes",
    "event", "events", "expiry_basis", "facility", "flags", "fluent", "fluent_values", "fluents", "format",
    "graph", "graph_ref", "historical", "id", "in_force", "intent_id", "interface", "kind", "knowledge",
    "limits", "link", "linked_actions", "mapping", "matched_scope", "max_depth", "metric",
    "missing", "node_cap", "nodes", "ontology_class", "purpose", "quality", "reasons",
    "reference", "references", "relationship_types", "requested_depth", "requester", "result",
    "retrieval", "retrieval_rank", "scope", "seconds", "severity", "situation", "source", "source_host",
    "source_references", "source_system", "stale", "state", "status", "subject", "template_id", "text",
    "topic", "truncated", "type", "unresolved_actions", "value", "version", "window", "window_seconds",
    "withheld", "provenance", "cut_by",
    "hypotheses", "hypotheses_truncated", "hypotheses_withheld", "label", "promoted_conflicts", "promoted_id",
    "rule", "support",
)


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _build_context() -> dict:
    terms: dict = {}
    for name in _PLAIN_TERMS:
        terms[name] = "cb:" + _camel(name)
    terms["class"] = "cb:contextClass"
    for name, iri in _DUBLIN_CORE.items():
        terms[name] = iri
    for name in _TIME_TERMS:
        terms[name] = {"@id": "cb:" + _camel(name), "@type": "xsd:dateTime"}
    terms["observed_at"] = {"@id": "prov:generatedAtTime", "@type": "xsd:dateTime"}
    for name in _LINK_TERMS:
        terms[name] = {"@id": "cb:" + _camel(name), "@type": "@id"}
    for name in _JSON_TERMS:
        terms[name] = {"@id": "cb:" + _camel(name), "@type": "@json"}
    context: dict = {
        "@version": 1.1,
        "@base": BASE_IRI,
        "cb": NAMESPACE,
        "prov": "http://www.w3.org/ns/prov#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
        "dcterms": "http://purl.org/dc/terms/",
    }
    for name in sorted(terms):
        context[name] = terms[name]
    return context


CONTEXT: dict = _build_context()

# Where a type (and an identity) is injected: (path, @type, native key copied to @id or None). A path is
# dot-separated native keys; a trailing [] on a key means every item of that list; the empty path is the
# document root. A path whose value is null or absent is skipped. Nothing is injected inside a value whose
# term is @json.
TYPE_PATHS: tuple[tuple[str, str, str | None], ...] = (
    ("", "cb:ContextResponse", None),
    ("situation", "cb:Situation", "id"),
    ("situation.scope", "cb:Scope", None),
    ("eligibility", "cb:Eligibility", None),
    ("fluent_values[]", "cb:FluentValue", None),
    ("fluent_values[].provenance", "prov:Entity", None),
    ("envelopes[]", "cb:Envelope", None),
    ("envelopes[].quality", "cb:Quality", None),
    ("envelopes[].provenance[]", "prov:Entity", None),
    ("graph", "cb:ContextGraph", None),
    ("graph.nodes[]", "cb:Node", "id"),
    ("graph.edges[]", "cb:Edge", None),
    ("graph.limits", "cb:GraphLimits", None),
    ("graph.hypotheses[]", "cb:Hypothesis", "promoted_id"),
    ("graph.hypotheses[].support[]", "cb:SupportRef", None),
    ("graph.hypotheses[].provenance", "prov:Entity", None),
    ("events", "cb:EventsResult", None),
    ("events.window", "cb:EventWindow", None),
    ("events.events[]", "cb:Event", "event_id"),
    ("events.events[].provenance", "prov:Entity", None),
    ("events.envelopes[]", "cb:EventEnvelope", None),
    ("events.envelopes[].quality", "cb:Quality", None),
    ("events.envelopes[].provenance[]", "prov:Entity", None),
    ("events.configured", "cb:ConfiguredSide", None),
    ("events.conflicts[]", "cb:Conflict", None),
    ("events.promoted_conflicts[]", "cb:PromotedConflict", "promoted_id"),
    ("events.promoted_conflicts[].support[]", "cb:SupportRef", None),
    ("events.promoted_conflicts[].provenance", "prov:Entity", None),
    ("knowledge", "cb:KnowledgeResult", None),
    ("knowledge.references[]", "cb:KnowledgeReference", None),
    ("knowledge.references[].matched_scope", "cb:MatchedScope", None),
    ("knowledge.references[].linked_actions[]", "cb:ActionReference", None),
    ("knowledge.references[].provenance", "prov:Entity", None),
)

_STEP = re.compile(r"^([a-z_]+)(\[\])?$")


def _steps(path: str) -> tuple[tuple[str, bool], ...]:
    if not path:
        return ()
    steps = []
    for part in path.split("."):
        match = _STEP.match(part)
        if match is None:
            raise ValueError(f"bad type path: {path!r}")
        steps.append((match.group(1), match.group(2) is not None))
    return tuple(steps)


_ROWS = tuple((_steps(path), type_, id_key) for path, type_, id_key in TYPE_PATHS)


def _targets(node, steps):
    """The dicts found at a path under `node`; a missing, null or wrongly shaped value yields nothing."""
    if not steps:
        if isinstance(node, dict):
            yield node
        return
    if not isinstance(node, dict):
        return
    (key, many), rest = steps[0], steps[1:]
    child = node.get(key)
    if child is None:
        return
    if many:
        if isinstance(child, list):
            for item in child:
                yield from _targets(item, rest)
    else:
        yield from _targets(child, rest)


def to_jsonld(response: ContextResponse) -> dict:
    """The profile document of a response. The response is not changed."""
    native = response.model_dump(mode="json", by_alias=True)
    for steps, type_, id_key in _ROWS:
        for target in _targets(native, steps):
            added = {"@type": type_}
            if id_key is not None and target.get(id_key) is not None:
                added["@id"] = target[id_key]
            rest = list(target.items())
            target.clear()
            target.update(added)
            target.update(rest)
    return {"@context": CONTEXT, **native}


def from_jsonld(doc) -> ContextResponse:
    """The native response of a profile document, or ValueError when it is not one.

    The document is not changed. Exactly the keys `to_jsonld` injects are removed; any other key is
    refused by the model (extra="forbid"), so nothing is silently dropped."""
    if not isinstance(doc, dict) or "@context" not in doc or doc["@context"] != CONTEXT:
        raise ValueError(_NOT_A_PROFILE)
    body = copy.deepcopy({key: value for key, value in doc.items() if key != "@context"})
    for steps, type_, id_key in _ROWS:
        for target in _targets(body, steps):
            if "@type" in target and target["@type"] != type_:
                raise ValueError(_NOT_A_PROFILE)
            if "@id" in target and (id_key is None or target["@id"] != target.get(id_key)):
                raise ValueError(_NOT_A_PROFILE)
            target.pop("@type", None)
            target.pop("@id", None)
    return ContextResponse.model_validate(body)
