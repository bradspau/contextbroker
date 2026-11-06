# Layer 3 gap analysis: Semantic Knowledge and Context

Compares the architecture's Layer 3 (`docs/AI_Ready_Network_Operations_Layer_Markdowns/03_Layer_3_Semantic_Knowledge_and_Context.md`) with what the broker implements on `fullnetwork` as of 2026-11-04.

The broker implements the Context Broker core (E01): resolve, join with observations, qualify, limit. It has part of the ontology and constraints (P07) and a graph platform with no temporal modelling. Governed knowledge retrieval was added in Phase 6 (tracked OKF bundles, a hybrid Qdrant index, a `knowledge` context class; see `design/knowledge.md`). Events were added in Phase 5 (a durable event store, a strict syslog parser and ingest command, an `events` context class and reconciliation against configured link state; see `design/event-store.md`). The rest of this analysis is as dated.

Basis: broker engine, envelope, resolvers and ontology files read closely; `intent_management`, `decision_records`, `loop_coordination` and `artefact_governance` skimmed.

## 1. Component mapping

| Layer 3 component | Spec candidate | In the broker today | Status |
|---|---|---|---|
| 4.1 Ontology, schema, constraints (P07) | OWL, RDFS, SHACL, Jena | `ontologies/tbox/topology.ttl` (`OperationalIntent`, `TopologicalLink`, `Interface`, `ServiceBinding`) and `ontologies/shacl/topology.shacl.ttl`, loaded by `ingestion/load_ontologies.py` into Fuseki | Partial. Small topology, intent and binding model. No competency questions, no source-terminology mappings (`data_contracts/metric_map.json` is the nearest), no ontology treatment of events, fluents or provenance |
| 4.2 Temporal knowledge graph (P08/P09) | Fluent patterns, named graphs, RDF-star, valid and knowledge time | Fuseki with persistent TDB2 holds topology and `:ServiceBinding` nodes | Platform only. Facts are current-state. The one stored history item is `:pinnedBound`, a single value rather than a fluent with valid time and knowledge time |
| 4.3 Observation and time-series store (S04) | VictoriaMetrics | VictoriaMetrics via PromQL in `broker_engine/tsdb_joiner.py`; `as_of` up to 30 days; point-in-time and worst-case-over-window queries in `broker_engine/temporal.py` | Built. Raw telemetry stays out of the graph, as the spec requires |
| 4.4 Portable curated knowledge (OKF) | Git OKF repo, Markdown, YAML | `knowledge/`: Markdown bundles with YAML front matter and an action catalogue file, read by a strict loader; four draft examples; approval is a reviewed commit (`design/knowledge.md`) | Built in Phase 6, for retrieval of references. No body-fetch route and no signed approval fields |
| 4.5 Lexical and vector retrieval | Qdrant, OpenSearch, pgvector | `okf_retrieval/`: Qdrant (own container, loopback, required key) with dense bge-small and sparse BM25 vectors fused by rank, models pinned by commit and baked into the image. The index only orders candidates; the files decide applicability | Built in Phase 6 |
| 4.6 Context Broker (A03, E01-E03) | FastAPI, SPARQL, MetricsQL, JSON-LD, MCP, REST | `broker_engine/`: FastAPI, SPARQL (`sparql_resolver`, `binding_resolver`), PromQL (`tsdb_joiner`), REST routes. MCP server built but not mounted | Built, narrower than the spec (section 2) |
| 4.7 Situation state (E02) | Durable workflow or state store | `IntentStore`, `decision_records`, `loop_coordination/reservations.py`, all in-memory | Weak. Continuity across assess, decision and the action-readiness check, but no durable or graph-shaped record and no expiry policy |

## 2. Context Broker steps (spec 4.6)

| Spec step | Broker today |
|---|---|
| 1. Interpret bounded request | Done. Typed and validated requests (safe tokens, window regex, `extra="forbid"`, `max_entities`, `as_of`). No purpose, requester or authority scope; the API is unauthenticated |
| 2. Resolve canonical identities | Partial. Intent id, device/interface and metric URIs resolve via `metric_map` and bindings. Identity is split across the two Jena instances, linked only by the intent id (`data_contracts/intent_bindings.py`) |
| 3. Traverse semantic knowledge | Minimal. intent, link, interface, device; `interface_neighbours` follows topology links. No generic depth-bounded traversal |
| 4. Select applicable fluent state | Not built |
| 5. Retrieve observations, aggregates, events | Observations and aggregates done (max, min, last, history, non-zero). Events built in Phase 5 (the parser and reconciler are no longer stubs; see `design/event-store.md`) |
| 6. Retrieve governed OKF knowledge | Built in Phase 6: the `knowledge` class returns the applicable artefacts as references, with status shown and draft never counted as approved (`design/knowledge.md`) |
| 7. Include approved-action references | Built in Phase 6 for artefacts that link to an action: an approved, in-scope template appears as a reference (id and version) in the knowledge result, seeded from `knowledge/action-catalogue.yaml`. Nothing executes or schedules one |
| 8. Compose a Context Graph | Partial. Output is a list of `ContextEnvelope`s plus a verdict, not a graph |
| 9. Evaluate freshness, completeness, conflict, authority | Mostly done, apart from authority. `build_observed` treats an unknown sample time as stale, the joiner flags multiple series as conflicts, missing data is never read as zero |
| 10. Return eligible context or explicit gap | Mostly done. `prune()` keeps unhealthy envelopes over healthy ones. Verdict is `success`, `failure` or `inconclusive`. No separate eligibility object or expiry boundary |

## 3. Context response fields (spec section 7)

| Spec field | In the broker |
|---|---|
| Situation identity | None |
| Entities and relationships | Partial (`entity_id`, `interfaces`) |
| Applicable fluent values | None |
| Observations or evidence references | Yes (`value`, `observed_at`, `source`) |
| OKF artefacts and versions | Yes, in the `knowledge` result: identifier, version, status, title and content hash per reference |
| Provenance | Free-form `provenance` dict, no standard |
| Quality indicators | Yes (`stale`, `missing`) |
| Unresolved conflicts | Yes (`conflicts`) |
| Missing evidence | Yes (`missing`) |
| Eligibility result and reasons | Partial. Reasons are fixed strings; no eligibility object |
| Expiry or freshness boundary | Partial. `DEFAULT_MAX_AGE_S` is 300 s; the response carries no explicit expiry |

## 4. Gaps in priority order

1. **Bitemporal fluents (spec section 5).** The largest gap, and spec risk 2 ("current state overwrites historically material fluent state"). Nothing in Fuseki records when a binding or topology link was valid or known.
2. **Context request and response contract (sections 6 and 7).** Add purpose, scope, authority and context classes to the request; an eligibility result and expiry to the response. Open decision: JSON-LD, NGSI-LD or the current purpose-built envelope.
3. **Durable situation state (E02).** Everything is in-memory, so continuity is lost on restart.
4. **Events (section 3).** Needs a real event store with semantic references, which means finishing the reconciler and syslog parser. *Built in Phase 5:* the store is in the state file, the parser is strict, a recognised link event is reconciled against configured link state at device level, and the details are in `design/event-store.md`. Semantic references beyond the device, inferred state from events and a live listener remain open.
5. **OKF and vector retrieval.** The largest new components and the least urgent while the demo is an intent-assurance loop. *Built in Phase 6:* governed knowledge is retrieved as references through a hybrid Qdrant index and checked against declared scope and version by deterministic code, with the details in `design/knowledge.md`. Still open: a route that returns the body of an artefact (`GET /context/knowledge/{id}/{version}`), applicability that checks live graph or situation facts beyond the declared scope, and signed approval fields in the front matter.
6. **Authentication and authority scope.** The REST API is unauthenticated, which conflicts with the access and authority scope in the request contract.

## 5. Where the broker already meets the spec

The broker's strictness matches the spec's verification list ("eligibility fail-closed behaviour", "conflict and missing-evidence behaviour"). The `ContextEnvelope` fails closed on unknown sample times, conflicting series and missing values, and the pruner never drops quality problems in favour of healthy data. It also uses the architecture's intended / configured / observed / inferred state categories.
