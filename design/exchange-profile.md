# The JSON-LD exchange profile of the context response (Phase 7)

How a consumer that wants linked data asks the broker for it, and exactly what comes back. This note
describes Phase 7 as built. It closes the JSON-LD half of gap 2 of `design/layer3-gap-analysis.md`;
NGSI-LD is not built and is listed under Limits below.

The profile is a **mapping of the native `ContextResponse`**, never a second source. The broker builds
the native answer first, records its situation row as it always did, and only then picks a
serialisation. Nothing in the profile is computed from a different read, so the two forms cannot
disagree about eligibility, expiry, the verdict or any evidence.

Code: `data_contracts/exchange_profile.py` (the context, the type table, `to_jsonld`, `from_jsonld` and
`negotiate`; standard library and the contract models only, no web framework and no JSON-LD library),
`broker_engine/main.py` (the one route that negotiates). Tests: `tests/test_exchange_profile.py`,
`tests/test_context_jsonld.py` and, for this note, `tests/test_design_notes.py`.

## Namespace and base IRIs

| Name | Value | Use |
|---|---|---|
| `NAMESPACE` | `https://example.org/ns/context-broker#` | prefix `cb:`; every broker term and every broker type |
| `BASE_IRI` | `https://example.org/ns/context-broker/id/` | the `@base`; a native node, situation, event or promoted id becomes one IRI under it |
| `PROFILE_MEDIA_TYPE` | `application/ld+json` | the media type a caller asks for and receives |

These are placeholders in the `example.org` space. The converter never reads them: `from_jsonld`
compares the whole `@context` with the one it holds and refuses any other, so nothing is looked up, and
no string in the context is a remote reference. A deployment that wants its own IRIs changes the two
constants and the fixtures together; a consumer must treat a changed namespace as a different profile.

## How to ask

Only `POST /context/request` negotiates. Send `Accept: application/ld+json`. The answer has status 200,
content type `application/ld+json` and the profile document. There is no new route (there is no
`/context/ld`), and the situation, the health and every other route answer JSON whatever the header says.

The choice is made by `negotiate(accept)`, a pure function that never refuses:

- The default is the native JSON. The profile is chosen only when it is acceptable (its `q` above 0) and
  the caller ranks it above `application/json`: a higher `q`, or an equal `q` named more specifically
  (an exact type beats `application/*`, which beats `*/*`). A tie at equal specificity keeps the native
  JSON, so `application/ld+json, application/json` is native.
- A header that is missing, empty, over 512 characters (`MAX_ACCEPT_LENGTH`) or over 32 comma-separated
  items (`MAX_ACCEPT_ITEMS`), or that names neither type, gives the native JSON with status 200. An
  item whose `q` is not a plain number from 0 to 1 (a sign, an exponent, `nan`, `inf`, an empty value)
  is ignored. Parameters other than `q`, such as `profile=`, are read and change nothing.
- An unsupported header is never a 406 and never a refusal. Every answer from this route, native or
  profile, carries `Vary: Accept`.
- Errors are always plain `application/json`. A refusal (401, 403, 400, 422) happens in the middleware
  and dependencies before the body runs, so asking for the profile cannot change a refusal, and it
  reaches no upstream.
- The situation id and the stored row are unaffected. The same request gives the same id whichever
  media type asked, and the row's repeat count grows by exactly one for each answer, not two.

Pinned examples (all in `tests/test_context_jsonld.py`):

| Accept | Answer |
|---|---|
| `application/ld+json` | profile |
| `application/ld+json, */*` | profile |
| `application/ld+json;q=0.9, */*;q=0.1` | profile |
| `application/json;q=0.5, application/ld+json` | profile |
| `APPLICATION/LD+JSON` | profile |
| (none), `*/*`, `application/*`, `application/json` | native |
| `application/ld+json, application/json` | native (tie) |
| `application/ld+json;q=0`, `;q=abc`, `;q=nan`, `;q=2`, `;q=-0.5`, `;q=1e0` | native |
| `application/ld+jsonx`, `text/html`, `,,,` | native |

## The context

The `@context` is inline and static, printed here as the code builds it. A consumer needs no fetch. A
test compares this block with `CONTEXT` and fails if either changes alone.

```json
{
  "@version": 1.1,
  "@base": "https://example.org/ns/context-broker/id/",
  "cb": "https://example.org/ns/context-broker#",
  "prov": "http://www.w3.org/ns/prov#",
  "xsd": "http://www.w3.org/2001/XMLSchema#",
  "dcterms": "http://purl.org/dc/terms/",
  "approved": "cb:approved",
  "authority_cut": "cb:authorityCut",
  "candidates": "cb:candidates",
  "candidates_withheld": "cb:candidatesWithheld",
  "cap": "cb:cap",
  "class": "cb:contextClass",
  "classes": {
    "@id": "cb:classes",
    "@type": "@json"
  },
  "configured": "cb:configured",
  "conflicts": "cb:conflicts",
  "content_hash": "cb:contentHash",
  "cut_by": "cb:cutBy",
  "depth": "cb:depth",
  "device": "cb:device",
  "edges": "cb:edges",
  "eligibility": "cb:eligibility",
  "end": {
    "@id": "cb:end",
    "@type": "xsd:dateTime"
  },
  "entities": "cb:entities",
  "entity_id": "cb:entityId",
  "envelopes": "cb:envelopes",
  "event": "cb:event",
  "event_id": {
    "@id": "cb:eventId",
    "@type": "@id"
  },
  "event_time": {
    "@id": "cb:eventTime",
    "@type": "xsd:dateTime"
  },
  "events": "cb:events",
  "expires_at": {
    "@id": "cb:expiresAt",
    "@type": "xsd:dateTime"
  },
  "expiry_basis": "cb:expiryBasis",
  "facility": "cb:facility",
  "flags": "cb:flags",
  "fluent": "cb:fluent",
  "fluent_values": "cb:fluentValues",
  "fluents": "cb:fluents",
  "format": "cb:format",
  "from": {
    "@id": "cb:from",
    "@type": "@id"
  },
  "graph": "cb:graph",
  "graph_ref": "cb:graphRef",
  "historical": "cb:historical",
  "hypotheses": "cb:hypotheses",
  "hypotheses_truncated": "cb:hypothesesTruncated",
  "hypotheses_withheld": "cb:hypothesesWithheld",
  "id": "cb:id",
  "identifier": "dcterms:identifier",
  "in_force": "cb:inForce",
  "ingest_time": {
    "@id": "cb:ingestTime",
    "@type": "xsd:dateTime"
  },
  "intent_id": "cb:intentId",
  "interface": "cb:interface",
  "kind": "cb:kind",
  "knowledge": "cb:knowledge",
  "known_at": {
    "@id": "cb:knownAt",
    "@type": "xsd:dateTime"
  },
  "label": "cb:label",
  "limits": "cb:limits",
  "link": "cb:link",
  "linked_actions": "cb:linkedActions",
  "mapping": "cb:mapping",
  "matched_scope": "cb:matchedScope",
  "max_depth": "cb:maxDepth",
  "metric": "cb:metric",
  "missing": "cb:missing",
  "node": {
    "@id": "cb:node",
    "@type": "@id"
  },
  "node_cap": "cb:nodeCap",
  "nodes": "cb:nodes",
  "observed_at": {
    "@id": "prov:generatedAtTime",
    "@type": "xsd:dateTime"
  },
  "ontology_class": "cb:ontologyClass",
  "promoted_at": {
    "@id": "cb:promotedAt",
    "@type": "xsd:dateTime"
  },
  "promoted_conflicts": "cb:promotedConflicts",
  "promoted_id": "cb:promotedId",
  "provenance": "cb:provenance",
  "purpose": "cb:purpose",
  "quality": "cb:quality",
  "reasons": "cb:reasons",
  "recorded_at": {
    "@id": "cb:recordedAt",
    "@type": "xsd:dateTime"
  },
  "reference": "cb:reference",
  "references": "cb:references",
  "relationship_types": "cb:relationshipTypes",
  "requested_depth": "cb:requestedDepth",
  "requester": "cb:requester",
  "result": "cb:result",
  "retrieval": "cb:retrieval",
  "retrieval_rank": "cb:retrievalRank",
  "retrieved_at": {
    "@id": "cb:retrievedAt",
    "@type": "xsd:dateTime"
  },
  "rule": "cb:rule",
  "scope": "cb:scope",
  "seconds": "cb:seconds",
  "severity": "cb:severity",
  "situation": "cb:situation",
  "source": "cb:source",
  "source_host": "cb:sourceHost",
  "source_references": "cb:sourceReferences",
  "source_system": "cb:sourceSystem",
  "stale": "cb:stale",
  "start": {
    "@id": "cb:start",
    "@type": "xsd:dateTime"
  },
  "state": "cb:state",
  "status": "cb:status",
  "subject": "cb:subject",
  "superseded_at": {
    "@id": "cb:supersededAt",
    "@type": "xsd:dateTime"
  },
  "support": "cb:support",
  "template_id": "cb:templateId",
  "text": "cb:text",
  "title": "dcterms:title",
  "to": {
    "@id": "cb:to",
    "@type": "@id"
  },
  "topic": "cb:topic",
  "truncated": "cb:truncated",
  "type": "cb:type",
  "unresolved_actions": "cb:unresolvedActions",
  "valid_at": {
    "@id": "cb:validAt",
    "@type": "xsd:dateTime"
  },
  "valid_from": {
    "@id": "cb:validFrom",
    "@type": "xsd:dateTime"
  },
  "valid_to": {
    "@id": "cb:validTo",
    "@type": "xsd:dateTime"
  },
  "value": "cb:value",
  "verdict": {
    "@id": "cb:verdict",
    "@type": "@json"
  },
  "version": "cb:version",
  "window": "cb:window",
  "window_seconds": "cb:windowSeconds",
  "withheld": "cb:withheld"
}
```

### Term rules

- Every wire key has an explicit term, and there is no `@vocab` catch-all: a key with no term is
  silently dropped by a JSON-LD processor, so a test walks the response models and fails on any wire
  key without one, and a second count of the triples taken from the document alone must equal the count
  a processor reports.
- A plain term is `cb:` plus the key in lowerCamelCase (`valid_at` is `cb:validAt`). `class` is
  `cb:contextClass`, because `class` is awkward in many languages.
- Times are typed `xsd:dateTime`: `expires_at`, `valid_at`, `known_at`, `retrieved_at`, `valid_from`,
  `valid_to`, `recorded_at`, `superseded_at`, `event_time`, `ingest_time`, `start`, `end` and
  `promoted_at`. `observed_at` maps to `prov:generatedAtTime`, the PROV-O generation time of the
  evidence. `retrieved_at` stays `cb:retrievedAt`, because PROV-O has no retrieval-time property.
- `identifier` and `title` map to `dcterms:identifier` and `dcterms:title`.
- Four keys are links, typed `@id`: `from`, `to`, `node` and `event_id`. This is what makes an edge a
  link between its two node IRIs. A value is the native string, and `@base` turns it into an IRI.
- Two keys are JSON literals, typed `@json`: `verdict` and `classes`. The verdict is free form, and
  `classes` is a map keyed by class name in the eligibility and a list in the situation; neither is a
  natural RDF structure. As a literal, their content is data: a key that starts with `@` inside a
  verdict is never read as a JSON-LD keyword, and a test proves no processor sees it as one.
- Nulls are kept in the document. Array order is the native order and means nothing more: ordered
  lists (events newest first, references by rank) carry the order in explicit fields.
- Promoted conflicts and hypotheses (see `design/semantic-promotion.md`) are included. A promoted
  conflict is typed `cb:PromotedConflict`. A hypothesis is typed `cb:Hypothesis`, with label
  `hypothesis` and state `inferred`, and it is a separate list beside the configured edges, never one of
  them.

## Types and identities

`TYPE_PATHS` is the only place an injected key is named. A path is dot-separated native keys, `[]` means
every item of a list, and the empty path is the document root. A path whose value is null or absent is
skipped, and nothing is injected inside a `@json` value.

| Path | `@type` | `@id` taken from |
|---|---|---|
| (document root) | `cb:ContextResponse` | (none) |
| `situation` | `cb:Situation` | `id` |
| `situation.scope` | `cb:Scope` | (none) |
| `eligibility` | `cb:Eligibility` | (none) |
| `fluent_values[]` | `cb:FluentValue` | (none) |
| `fluent_values[].provenance` | `prov:Entity` | (none) |
| `envelopes[]` | `cb:Envelope` | (none) |
| `envelopes[].quality` | `cb:Quality` | (none) |
| `envelopes[].provenance[]` | `prov:Entity` | (none) |
| `graph` | `cb:ContextGraph` | (none) |
| `graph.nodes[]` | `cb:Node` | `id` |
| `graph.edges[]` | `cb:Edge` | (none) |
| `graph.limits` | `cb:GraphLimits` | (none) |
| `graph.hypotheses[]` | `cb:Hypothesis` | `promoted_id` |
| `graph.hypotheses[].support[]` | `cb:SupportRef` | (none) |
| `graph.hypotheses[].provenance` | `prov:Entity` | (none) |
| `events` | `cb:EventsResult` | (none) |
| `events.window` | `cb:EventWindow` | (none) |
| `events.events[]` | `cb:Event` | `event_id` |
| `events.events[].provenance` | `prov:Entity` | (none) |
| `events.envelopes[]` | `cb:EventEnvelope` | (none) |
| `events.envelopes[].quality` | `cb:Quality` | (none) |
| `events.envelopes[].provenance[]` | `prov:Entity` | (none) |
| `events.configured` | `cb:ConfiguredSide` | (none) |
| `events.conflicts[]` | `cb:Conflict` | (none) |
| `events.promoted_conflicts[]` | `cb:PromotedConflict` | `promoted_id` |
| `events.promoted_conflicts[].support[]` | `cb:SupportRef` | (none) |
| `events.promoted_conflicts[].provenance` | `prov:Entity` | (none) |
| `knowledge` | `cb:KnowledgeResult` | (none) |
| `knowledge.references[]` | `cb:KnowledgeReference` | (none) |
| `knowledge.references[].matched_scope` | `cb:MatchedScope` | (none) |
| `knowledge.references[].linked_actions[]` | `cb:ActionReference` | (none) |
| `knowledge.references[].provenance` | `prov:Entity` | (none) |

## The converter and its fixtures

`to_jsonld(response)` returns `{"@context": CONTEXT, ...}` over the native JSON, by alias exactly as the
route would serialise it, with the `@type` and `@id` keys injected at the paths above. The response it
is given is not changed.

`from_jsonld(doc)` is the inverse and is strict. It refuses (`ValueError`) a document whose `@context`
is missing or differs from `CONTEXT`, a wrong injected `@type`, and an `@id` that differs from its
native id field. It removes exactly the injected keys, by the same walker, and validates the rest
against the native model, which forbids extra keys, so nothing is silently dropped.

The known-answer fixtures under `tests/fixtures/exchange/` are tracked in git: `response-native.json` and
`response.jsonld`, the same response with all five classes, a verdict, a promoted conflict and a
hypothesis. The tests check both directions against them, check the helper writes exactly those files,
and parse `response.jsonld` with rdflib with no network. Its triple count is pinned at 291, and an
independent count made from the document alone is 291 as well. The fixtures hold demo names and fixed
times only.

## Limits

- No NGSI-LD. The profile is JSON-LD only; an NGSI-LD view is deferred.
- No `/context/ld` route and no second endpoint: the media type is chosen on the one existing route.
- No remote context. The context is never fetched or served, so there is no URL to pin or to cache.
- The profile adds no field of its own. A consumer that needs a value finds it in the native answer.
- Asking for the profile grants nothing. Authority, scope and the claims are checked before the body
  runs, exactly as for the native JSON.
