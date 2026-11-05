# The context request and response contract

How a caller asks the broker for context and what comes back. This note describes Phase 3 as
built. It closes gap 2 of `design/layer3-gap-analysis.md` (no purpose, scope or authority in the
request; no situation identity, eligibility result, expiry, structured provenance or graph in the
response). The gap analysis is a dated document and is left as written; where it says the
provenance is a free-form dict or that no eligibility object exists, this note supersedes it.

The broker resolves, joins and delivers context. It never decides or acts, and nothing in the
response grants authority to act. Verdicts stay `success`, `failure` or `inconclusive`.

Code: `data_contracts/context_contract.py` (the contract, no I/O, no web framework),
`broker_engine/context_request.py` (the one handler that reads the sources),
`broker_engine/context_eligibility.py`, `broker_engine/context_graph.py`,
`broker_engine/auth.py` and `scripts/mint_token.py` (authority). Phase 4 stores situations and
Phase 7 adds exchange profiles; both build on the decisions below.

## The request

One route, `POST /context/request`. The five older routes stay as primitives and their fate is
not decided here.

| Field | Rule |
|---|---|
| `purpose` | One of `assurance`, `diagnosis`, `reporting` (a closed set kept in `context_contract.PURPOSES`). |
| `scope` | A device and optionally an interface; letters, digits, `_`, `-`, `/`, at most 128 characters each. |
| `intent_id` | Optional text in the TMF921 client's grammar; the intent whose goals and placements are wanted. |
| `classes` | One to three of `topology`, `goals`, `observations`, no repeats. |
| `max_depth`, `relationship_types` | The traversal. Depth 0 to 3; types from `has_interface`, `connected_to`. |
| `as_of`, `known_at` | Offsets such as `10m`, resolved against the single clock read of the request; limited by the retention period (400 beyond it). |
| `freshness` | `1s` to `300s`; it can only tighten the default, a larger value is a 400, not a clamp. |

There is no requester field. The requester is the token's `sub`, so a body cannot claim to be
someone else. The model is strict: unknown fields and values of the wrong type are refused (422).

Coherence rules, all refused by shape before any I/O: `topology` exactly when both `max_depth` and
`relationship_types` are given; `intent_id` exactly when `goals` is asked for; `observations` only
together with `goals`, because the series to read come from the intent's placements.

The logical contract is defined apart from its wire format. The JSON form is purpose-built nodes
and edges; JSON-LD and NGSI-LD views of the same content are Phase 7 and need no change to it.

## Authority

Authority is a signed claim in the token, so the broker stays stateless (verify only, no list of
its own):

```json
"authority": {"entities": ["edge-sw-1"], "purposes": ["assurance"], "classes": ["topology", "goals"]}
```

- Entities are exact device names or the single string `*`. There is no pattern matching of any
  kind. Purposes and classes come from the closed sets.
- `scripts/mint_token.py` takes `--entity`, `--purpose` and `--class`, all three or none, and
  refuses an entity that is not `*` or a safe device name. The broker parses the claim totally:
  a malformed list, a wrong type or a list left empty after filtering makes the whole claim
  unusable, never partly granted.
- A token with the scope but no usable claim is refused on this route only, with 403 `authority
  claims required`. Old routes keep working on their old scopes, so no token has to be reminted for
  them.
- The check has two steps and both happen before the first Fuseki, VictoriaMetrics or TMF921 call.
  Route dependencies check the `context:read` scope, then the presence of a usable claim. The
  handler then calls `authorize`, which reports entity, then purpose, then class (`outside
  authority scope: entity`, `... purpose`, `... class`), and requires `context:assess` as well
  when an `intent_id` is named. Offsets and freshness are resolved (and refused) in the same
  place.
- A token whose `sub` is empty, over 128 characters or not printable is 401 on every `/context/*`
  route: anonymous callers would otherwise share one requester and so one situation identity.

Authority also covers what traversal discovers, not only the scope in the body. After the Fuseki
read and before any VictoriaMetrics or TMF921 call, devices the entities do not cover are dropped,
including devices reached only through an intent's placements. The Context Graph then reports
`authority` in `cut_by`. A placement on an uncovered device is not delivered as a fluent value, its
metric is not read, the goals class is ineligible (`placement outside authority scope: <Metric>`)
and the verdict is `null`. A candidate on an uncovered device names `authority` only, even when its
relationship type was not requested, so `relationship_types` never reveals an edge to a hidden
device. One bit still leaks: the conflicting-links finding is computed over every in-force link
record read for the frontier, including one whose far end is hidden. `authority` in `cut_by`
already says something lies beyond, so this was accepted.

## Situation identity

`situation.id` lets Phase 4 store outcomes and decisions against a question. The inputs are the
requester, purpose, scope device and interface, intent id, the valid instant, the knowledge
instant, the sorted classes, `max_depth` and the sorted relationship types.

Construction (`situation_id`, `SITUATION_VERSION = 1`): build a JSON object with the keys `v`,
`requester`, `purpose`, `scope`, `intent`, `valid`, `known`, `classes`, `max_depth` and
`relationship_types`; serialise it with sorted keys, no spaces and ASCII escaping; take the sha256;
the id is `sit-` followed by the first 32 hex characters (128 bits).

- The scope is a list, `[device, interface]`, never a joined string, so `a/b` as a device cannot
  collide with device `a` and interface `b`.
- Both instants are floored to the second in UTC. An unset knowledge instant is the JSON `null`
  ("latest"), never "now", so asking for the latest record is a stable meaning. A request with an
  offset resolves to an absolute instant, so the same offset asked a second later is a different
  situation.
- A known-answer test pins one id against hand-written canonical JSON (not against the function
  that produces it): requester `alice`, purpose `diagnosis`, device `Router-A`, valid
  `2026-11-05T12:00:07Z`, classes `["topology"]`, depth 2, types `["connected_to",
  "has_interface"]` gives `sit-178907b20344e21c79bdc9c57b918ef7`.

Phase 4 stores these ids and node ids. The inputs, their order of encoding and the node id rules
below must not change without a `SITUATION_VERSION` bump and a plan for matching old records.

## Eligibility

A fitness label for the evidence, per requested class and overall. Three results: `gap` (evidence
is absent), `ineligible` (present but unfit), `eligible` (every applicable item fresh, finite,
single-valued and unchanged). Precedence is ineligible over gap over eligible; each class keeps its
own reasons and the overall reasons are their sorted union. Only requested classes are judged, so a
topology-only request is never made ineligible by a stale metric it did not ask for. Reasons are
fixed strings; the only variable part is a pinned metric short name.

| Class | Result | Reason |
|---|---|---|
| goals | gap | `no binding for intent`; `no goal in force: <M>`; `no placement in force: <M>` |
| goals | ineligible | `invalid binding`; `duplicate goal in force: <M>`; `duplicate placement in force: <M>`; `goal changed within window: <M>`; `placement changed within window: <M>`; `unmapped metric: <M>`; `placement outside authority scope: <M>` |
| topology | gap | `scope entity not in the topology` |
| topology | ineligible | `ambiguous identity: scope entity`; `ambiguous identity: graph node`; `conflicting link records`; `invalid topology record` |
| observations | gap | `no observation: <M>` (absent, missing, no value or not a finite number) |
| observations | ineligible | `conflicting observation: <M>`; `stale observation: <M>`; `future-dated observation: <M>` |

- The evidence window for goals is the freshness limit before the valid instant. Changed goal means
  more than one distinct goal fluent in force over that window (for placements likewise). It needs
  no TMF921 read, so a past request still reads the graph only. A correction (the earlier record
  superseded, the same interval reissued) leaves one fluent known at any knowledge time, so it is
  not a change. A duplicate in force suppresses the changed reason for the same metric.
- The freshness limit is 300 s unless the request tightened it. The joiner judges staleness
  against 300 s only, so the observations class also applies the tightened limit itself. A sample
  with no observation time cannot be shown fresh and is ineligible. Future-dated means more than
  5 s after the valid instant.
- A non-finite sample stays `missing` in the joiner and classifies as `gap` here. It is never
  eligible and the joiner is unchanged.
- Eligibility grants no execution authority. The action-readiness check is a separate component and
  nothing in this response feeds it.

### Why eligibility and the verdict are kept apart

The verdict answers "does the evidence meet the intent"; eligibility answers "is the evidence fit
to be used at all". The module that computes eligibility imports nothing from the verdict path (an
AST test pins this), the verdict is computed before the eligibility is combined, and tests patch
each side to show neither sees the other. They can disagree by design. Example: a goal was pinned
at 100 and the live goal is now 150. Eligibility reads the stored fluents only, finds one goal in
force and says the goals class is eligible; the verdict compares the live goal with the pinned one
and is `inconclusive` ("goal changed: LatencyMetric pinned 100, now 150"). A caller reads both.

## Expiry

`expires_at` is when the answer stops being reusable as current context; `expiry_basis` names why.

| Basis | Request | `expires_at` |
|---|---|---|
| `oldest_observation` | current, with at least one observation time | oldest observation time plus the limit |
| `configured_state` | current, only topology or fluent values found | retrieval time plus the limit |
| `no_evidence` | current, nothing found | retrieval time |
| `historical_request` | `as_of` or `known_at` given | retrieval time |

The limit is the 300 s default or the tightened freshness. It is never clamped to the future: an
oldest observation that is already stale gives an expiry in the past, which is the honest answer.
Configured evidence is anchored to the retrieval time because a fact's `recordedAt` says when the
broker learned it, not how old the fact is. A past answer expires at retrieval because a historical
answer is not current context, and the response says so through the basis.

## Provenance records

Every envelope's `provenance` is a list of records with four fields: `source_system`, `reference`,
`observed_at`, `retrieved_at`. The reference is built by the broker from pinned names (the series
name `interface_metrics_<metric>`, a fluent IRI, a component name and version), never from text in
an upstream answer. An envelope with no provenance serialises `[]` on every route.

- The joiner stamps exactly one record on every envelope it returns, including empty, NaN,
  unparsable and multi-series answers. Its `retrieved_at` is the caller's `now`, read before an
  `as_of` shifts the query instant.
- A fluent value carries one record (`fuseki`, the fluent IRI, observed at the fluent's
  `recordedAt`).
- The model client stamps a model record first and then one record per input envelope. The event
  reconciler stamps one syslog record.
- Fields left the model client and reconciler provenance when the dict became records: the always
  empty `uncertainty` and the `input_window` object (the inputs are now per-input records), and
  the reconciler's raw device text and severity. Phase 5 owns event storage and semantic
  references; Phases 5 and 7 add back whatever they need in their own structures.

## The Context Graph

Nodes and edges with canonical ids.

- A device node id is the `:onDevice` name (`edge-sw-1`); an interface node id is
  `device/interface`. The interface IRIs in the graph look like `if-router-a-ge000`, so the IRI
  local name is carried as `graph_ref` on the interface node. Other IRI-backed things (links and
  link fluents on edges) use their IRI local names.
- Edges: `has_interface` (device to interface) and `connected_to` (interface to interface, with
  `from` and `to` in sorted id order and the `link` and `fluent` local names). One link fluent value
  accompanies each returned `connected_to` edge.
- Depth counts hops from the scope. Device scope: device 0, its interface 1, the peer interface 2,
  the peer device 3. Interface scope: interface 0, its device and the peer interface 1, the peer
  device 2.
- Four limits can cut a result, and `cut_by` lists the ones that did: `depth`,
  `relationship_types`, `node_cap` and `authority`. The hard maximum depth is 3 (refused at
  validation, so it is never a cut reason) and the node cap is 50. At the depth edge a look-ahead
  round finds whether anything lies beyond; it decides only depth, so at depth 1 with only
  `has_interface` the result has an empty `cut_by` although a link exists beyond. The look-ahead
  adds edges between nodes already in the graph, never a node, so every in-force link between two
  kept nodes is an edge.
- Under the node cap, candidates are ordered per level by priority and then by id. Priority nodes
  are the interfaces that carry an envelope and their devices. Priority only reorders within a
  level: a priority node deeper than the level where the cap bites is not reserved a slot, and one
  out of reach of the scope never enters the graph. Envelopes for every returned node are kept, and
  an unhealthy one is never dropped in favour of a healthy one.
- Findings are flags, not text: a scope that is missing from the topology (gap, empty graph), an
  ambiguous scope (empty graph), two interfaces mapping to one node id (collision), two in-force
  fluents on one link or a link naming other than two usable interfaces (conflicting links, both
  edges returned), and a malformed record. None of them is merged or guessed; each makes the
  topology class ineligible or a gap. A link fluent with no `:validStart` or `:recordedAt` is
  invisible to the query and cannot be flagged.
- Reads: SPARQL answers one level (one device query, one link query) and the limits are policy in
  Python. A device's interfaces are cached for the walk, so there are at most `2 x (max_depth + 1)`
  queries. Link fluents are read with no time filter and selected in Python with the fluent's
  `covers` and `known` rules, so valid time and knowledge time are decided in one place. Every
  value read back is checked against the token and URI grammars before it is used in a later query.
- The link query contains a nested `OPTIONAL`. It is correct under the SPARQL algebra and passes on
  the rdflib stand-in. Three parity tests in `tests/test_fuseki_parity.py` run the same readers
  against a real engine; they are skipped unless `FUSEKI_PARITY_URL` names a throwaway dataset
  (never the persistent store), and they are the check on Jena.

## The verdict

Present only when `intent_id`, `goals` and `observations` are all asked for, else `null`. It comes
from `assess_tmf921_as_of`, the read-only assess, never the pushing one. A context request calls
none of the code that pushes an observation, writes a high-water mark, updates a fulfilment
outcome or records an outcome; tests spy on each.

- It is judged on the same reads as the eligibility. The handler reads the intent's fluents from
  Fuseki once, picks the binding from that snapshot and passes it to the assess as the binding, and
  the observation reads go through a recording joiner so each distinct telemetry read happens once
  and both sides see its envelope.
- A current request also makes one read-only `GET` to the TMF921 server (the intent's status and
  live goal, which is what produces the changed-goal reason above). A past request makes none: it
  judges the goal and placement in force then, as known then, from the graph.
- One exception to "one binding read": when the binding is invalid (two placements or goals in force,
  or the metric map fails to load), the handler passes no binding and the assess reads it itself and
  refuses with a fixed reason. Both are error paths and the second read is documented, not a
  defect.
- The verdict is `null` when a placement of the selected binding is on a device the caller's
  entities do not cover, because judging it would read that device.
- The response drops the assess keys `context` and `goals`, which duplicate `envelopes` and
  `fluent_values`; it keeps `verdict`, `per_interface`, `reasons`, `dropped_count`, `intent_status`,
  `last_outcome`, `source`, `mode`, `as_of`, `evaluated_at` and `requirement`, plus `goal_changes`
  and `known_at` when they apply.

## Deferred

- The fate of the five older routes (keep, deprecate, remove): decided once the new route is
  proven.
- JSON-LD and NGSI-LD views of the Context Graph: Phase 7, as an exchange profile over this
  content.
- Storing situations and decision records against `situation.id`: Phase 4.
- Adoption by the MCP server and the agent tool (unchanged here; the MCP server is not mounted).
- `/docs` and `/openapi.json` are still public (bead `contextbroker-8at`). A test checks that the
  OpenAPI document, which now describes the new route, holds no claim value, token or secret.
- Event storage and semantic references on provenance: Phase 5.
