# Governed knowledge: retrieval, approval, applicability and the knowledge class (Phase 6)

How the broker finds governed knowledge for a context request, which artefacts it may return, and
what the response carries. This note describes Phase 6 as built. It closes gap 5 of
`design/layer3-gap-analysis.md` for the parts listed in the Deferred section below. Later phases
(the exchange profiles of Phase 7) read the same references, so the rules here must not change
without a plan for the situation rows already stored.

Code: `data_contracts/knowledge.py` (vocabulary and the applicability rules, no I/O),
`okf_retrieval/loader.py` (the strict file loader), `okf_retrieval/config.py` (settings and the model
pins), `okf_retrieval/embedder.py` and `okf_retrieval/index.py` (the models and the Qdrant collection),
`okf_retrieval/service.py` (one retrieval, every fault turned into one outcome),
`data_contracts/context_contract.py` (the class, the topic and the response models),
`broker_engine/context_eligibility.py` and `broker_engine/context_request.py` (the knowledge branch).
The tracked files themselves are in `knowledge/` (its `README.md` is the author's guide).

## Purpose and boundary

The broker returns **references** to governed artefacts: identity, version, status, title, content
hash, the scope that matched, source references, a position in the list and the approved actions the
artefact links to. It does not return the body of an artefact.

- It never decides applicability by similarity. A ranking index only orders candidates; the loaded
  files and deterministic code decide whether each one applies (OKF-02).
- It never approves. Approval is a person's reviewed commit. The broker reads the `status` field and
  never writes it (OKF-01).
- It never acts. A linked action is a reference to an approved template. Nothing in the broker
  executes, schedules or submits one, and the knowledge path imports none of the executor, the
  action-readiness check or the loop coordination code (an import test pins this) (OKF-03).
- The knowledge class is judged on its own, like the other classes. It never feeds the verdict, the
  verdict is computed before the class is read, and it grants no execution authority.

## Retrieval product

Decided in the discuss step of this phase (D-35, D-36): **Qdrant**, in its own container, with hybrid
search. The options on the table were Qdrant, pgvector and OpenSearch for the index, and a local model,
a hosted embedding API or lexical search only for the vectors.

- Why Qdrant fits here: one small container with one HTTP port, dense and sparse vectors in the same
  collection, and a single query that runs both searches and fuses them by rank. pgvector would need a
  Postgres server the stack does not otherwise have (the broker's state is one SQLite file).
  OpenSearch would add a JVM search cluster for a few dozen files. A hosted embedding API would send
  the text of the artefacts off the machine and need a secret, which D-36 rules out.
- The container is `qdrant/qdrant:v1.19.2-unprivileged`, published on `127.0.0.1:6333` only (the gRPC
  port is not published), with a required API key and telemetry off. Its data is the `qdrant-data`
  volume.
- **Hybrid search.** Dense vectors (BAAI bge-small-en-v1.5, 384 dimensions, in fastembed's quantised
  copy `Qdrant/bge-small-en-v1.5-onnx-Q`) and sparse BM25 vectors (`Qdrant/bm25`) are queried in one
  call and fused by reciprocal rank fusion, with no weights and **no filter**. A request asks for the
  top 20 candidates (`KNOWLEDGE_CANDIDATES`).
- **Models are local and pinned.** The two model repositories are pinned by commit
  (`aa8f8b060edb00e03bfdd08813a2949946c8ba55` and `22b8d2af71a76161e18dd432d2cee0eefa66e412`, named
  in `okf_retrieval/config.py`). The image build downloads them into `/opt/models` and a test keeps
  the Dockerfile and the config equal. The image sets `HF_HUB_OFFLINE=1`, and the embedder checks
  that the model directories exist before it imports the model library, so no run-time call to a model
  host is possible and no secret is involved (D-36). Tests use a fake embedder, so the suite needs no
  model.
- **The index is derived data.** Its collection is named `knowledge-` plus 12 hex characters of a hash
  over the embedder fingerprint, the dimension and the sorted identity triples (identifier, version,
  content hash) of the loaded files. A collection with that name is current by construction; a
  missing one is rebuilt from the files on the next request, older `knowledge-` collections are
  removed after a successful build, and a build that fails leaves no half-built collection. Each point
  holds only the identifier, the version and the content hash. Text goes in as vectors, never as a
  stored payload, and the files hold everything the index does.

## Bundles

Per D-38, D-39 and D-41. A bundle is one Markdown file with YAML front matter, in the tracked
`knowledge/` directory. The API container sees it read-only at `/knowledge` (`BROKER_KNOWLEDGE_PATH`).
Git is the source of truth.

- **Layout and identity.** `knowledge/bundles/<identifier>/<version>.md`. The directory name is the
  identifier and the file stem is the version, and both must equal the `identifier` and `version` in the
  front matter. An identifier is 3 to 64 lower-case letters, digits, dots and hyphens. The version is
  quoted in the front matter, and versions order numerically (`1.10` is after `1.2`).
- **Front matter.** `identifier`, `version`, `status`, `title`, `scope` (`entities`, `purposes`, and
  optionally `ontology_classes`), `source_references` and `linked_actions`. A source reference is
  `repo:<tracked path>`, `metric:<pinned metric short name>` or `ontology:<class>`. The model is strict:
  an unknown key, a wrong type and a duplicate key are all refusals.
- **The strict loader.** Every file is checked before it counts. The fixed refusal reasons are: not
  utf-8, byte order mark, carriage return, too large, symlink, not a bundle file, no front matter,
  duplicate key, not yaml, invalid front matter, path mismatch, duplicate version, invalid version, no
  body, body too long, no bundles directory, too many files and invalid catalogue. Limits: 64 KiB a file,
  6000 characters of body, 500 files. **One refused file blocks its whole identifier**, so an older valid
  version is never served in place of a newer one that failed to load. The refusal is logged as a fixed
  line that names no file.
- **Content hash.** `sha256:` plus 64 lower-case hex over the exact bytes of the file. Because the hash
  covers the bytes, line endings matter: `.gitattributes` sets `knowledge/** text eol=lf`, and the
  loader refuses a carriage return. A reviewer can recompute the hash and compare it with the one in a
  response.
- **Statuses.** `draft`, `reviewed`, `approved` and `withdrawn`. The first three are returned with their
  status shown; `withdrawn` is never returned.
- **Approval** is the owner changing `status` in a reviewed commit (D-41). There is no approval workflow
  in code, no field the broker writes and no signed approval field (that is deferred).
- **Changes need a restart.** The files are loaded once per service life and a load failure is kept for
  that life, so a change to a file takes effect when the API restarts. The embedder, the client and the
  collection are retried on each request until they succeed.
- **Examples.** Four draft bundles ship, in this project's own words: interface-error handling,
  latency-goal investigation, topology-change check, and a decoy scoped to a device that does not exist
  in the demo (it proves a request for another device does not get it).

## Applicability

Per D-43 and D-44. `data_contracts/knowledge.py` decides, from the declared scope only. No function
there takes a score, a rank or a similarity.

- **Entity patterns.** `*` (everything), an exact name, or a name with one trailing `*` as a prefix
  (`Router-*`). Case-sensitive, no `?`, no brackets. A pattern matches when it matches the scope device
  or the scope node id (`Router-A/GigabitEthernet0/0/0` for an interface scope).
- **Purposes.** The request purpose must be among the declared purposes.
- **Node class.** `ontology_classes` is optional; when declared, the scope node must be of a declared
  class (`Device` for a device scope, `Interface` for an interface scope). Empty means no constraint.
- **Latest version only.** Of the versions of an identifier only the highest is a candidate. A
  withdrawn latest version removes the identifier altogether, older versions included. A withdrawn older
  version is ignored.
- **The index ranks and the files decide.** The service takes up to 20 candidates from Qdrant in their
  fused order and keeps one only when its identifier is loaded, its version is the loaded latest, its
  content hash equals the loaded file's, it has not been seen already, and `applicable()` returns a
  match. A candidate that fails the first four checks is dropped with one fixed log line per
  retrieval. So a stale, wiped or tampered index can lose a result but can never add or alter one.
- **Draft results** are shown with their status. The broker does not sum drafts, or any combination of
  statuses, into an approval.
- **Withheld** (D-48). A candidate whose purpose and node class fit but whose entity patterns match
  nothing in the caller's token entities is counted, not named. The count is never true for a wildcard
  token, an applicable artefact or a withdrawn one.

## The knowledge class

Per D-37, D-42, D-45, D-47 and D-48.

- **Token class.** `knowledge` is the fifth value of the context classes and is part of the signed
  authority claim (`scripts/mint_token.py --class knowledge`). A token without it is refused with 403
  `outside authority scope: class` for a knowledge request, before any retrieval, upstream call or
  situation row. The class can be asked alone or with the others and adds no coherence rule.
- **Topic.** An optional `topic` string, 1 to 200 characters, letters, digits, space and `_ . , : / -`
  only, strict about type, and only with the knowledge class (`topic needs the knowledge class` is a
  422). It is the text sent to the index. With no topic the query is derived from the purpose and the
  node type (for example `diagnosis interface`).
- **Reference fields.** `identifier`, `version`, `status`, `title`, `content_hash`, `matched_scope`
  (the artefact's own patterns that matched, the purpose, the node class or null),
  `source_references`, `retrieval_rank` (a position from 1, not a score), `linked_actions` (template id
  and version, `approved` always true), `unresolved_actions` (a count, never named) and one provenance
  record (source `okf`, reference `<identifier>@<version>`, observed time null, the retrieval time).
- **What is never present.** No body, no excerpt and no ranking value. No model has a field for
  them (pinned by tests, including a recursive walk of a real response). The body is read from the
  repository; a route that fetches it is deferred.
- **Cap and truncation.** At most 10 references (`KNOWLEDGE_RESULT_CAP`); `truncated` is true when more
  applied. `withheld` is a count.
- **Three retrieval outcomes**, and the eligibility each gives:

  | Outcome | Meaning | Eligibility | Fixed reason |
  |---|---|---|---|
  | `answered`, no reference | retrieval ran, nothing applies | gap | `no applicable knowledge` |
  | `unavailable` | retrieval could not run | gap | `knowledge retrieval unavailable` |
  | `not_current` | the request names a past time | gap | `knowledge is current only` |
  | `answered`, 1 or more | | eligible | none |

  Draft status, truncation and withheld are not reasons.
- **Knowledge is current only.** The files hold no history, so a request with `as_of` or `known_at`
  gets `not_current` without any retrieval, and nothing is said about what was true then.
- **Failure.** The knowledge branch fails closed with the fixed reason `knowledge retrieval unavailable`
  when Qdrant, the models or the files cannot be used: any exception in the retrieval becomes the
  outcome `unavailable` with one log line naming only the exception class. The answer is still a 200,
  the other requested classes answer as usual and the verdict is identical with and without the class
  (a test compares them). The API itself starts without Qdrant or the models.
- **Expiry.** A current answer that returned at least one reference expires at the retrieval time plus
  the freshness limit, with the basis `knowledge_set`. Precedence among the six bases:
  `historical_request`, `oldest_observation`, `event_window`, `knowledge_set`, `configured_state`,
  `no_evidence`. The files carry no age to count from, which is why it is anchored to the retrieval time.
- **Situation id.** The topic enters the canonical body as a conditional `topic` key, only when set
  (the way `window` does for events), so every earlier id is byte for byte what it was and
  `SITUATION_VERSION` stays 1. Two new known answers pin the knowledge ids: requester `alice`, purpose
  `diagnosis`, device `Router-A`, valid `2026-11-05T12:00:07Z`, classes `["knowledge"]` gives
  `sit-bb260446e080cfec31f5fe1ebe6afb3d` without a topic and `sit-5d99dd6640b6fd47a02597c2f0264328` with
  the topic `interface errors`. Each topic is its own situation.
- **Stored situation.** The row keeps one `okf` provenance record per returned reference and the topic.
  It holds no title, hash, body or action text.

## Action references

Per D-46 and D-49.

- **The catalogue file** `knowledge/action-catalogue.yaml` seeds the existing `ActionCatalogue` with
  seven fields per template: `template_id`, `version`, `scope_pattern`, `preconditions`,
  `expected_outcome`, `recovery` and `approved`. It is read-only and loaded with the bundles.
- **Approved-latest semantics are reused unchanged.** A bundle links an action by template id, and
  `ActionCatalogue.get` returns the highest **approved** version of that id. The consequence: when
  version 1.0 is approved and a newer 1.1 is not, the reference names 1.0, so a newer draft of a
  template never replaces the approved one until it is approved itself. A link to an id with no
  approved version, or to an id the catalogue does not hold, adds one to `unresolved_actions`, and the
  id is never named.
- **Scope check.** A resolved template is kept only when its `scope_pattern` matches the scope device
  or node id (`fnmatch` rules, case-sensitive).
- **References only.** The response names the template id and version. Nothing in the broker runs,
  submits or schedules the action, there is no route or tool for it, and the action-readiness check is a
  separate component that nothing here feeds.
- **The example.** The catalogue holds `example-collect-interface-counters` 1.0, a read-only
  template marked `approved: true`, so one resolved reference shows end to end, and
  `example-restart-interface` 1.0, marked `approved: false`, which is never returned. The owner
  confirmed the approved entry in review on 2026-11-06.

## Operating it

- **Health.** `GET /health` has eight keys; `qdrant` is `answering` or `not answering`, probed at the
  origin of `BROKER_QDRANT_URL` plus `/readyz` with no credential and a 2 s timeout. A Qdrant that does
  not answer makes the body 503 with `status: degraded`. The body carries no URL, host, port or server
  text. The compose service of the API lists Qdrant in `depends_on` without a health condition, so
  Qdrant being down does not stop the API from starting.
- **The key.** `QDRANT_API_KEY` is required in `.env` (letters and digits only; for example the output
  of `openssl rand -hex 24`). Compose refuses to run without it, for every compose command, not only for
  the Qdrant service. Never print or commit it. The API and the Qdrant container read the same
  variable; the broker sends it to Qdrant and never returns it.
- **Settings.** `BROKER_KNOWLEDGE_PATH` (default the `knowledge` directory beside the package, `/knowledge`
  in the container), `BROKER_QDRANT_URL` (default `http://localhost:6333`, in compose
  `http://qdrant:6333`; an http or https URL with a host and no credentials, query or fragment) and
  `BROKER_EMBED_MODEL_DIR` (default `/opt/models`). A bad value names the variable and never echoes it.
- **State file.** No table is added and `SCHEMA_VERSION` is unchanged. Situation rows carry the new
  topic and may name the knowledge class.
- **Image rollback caveat.** The Phase 5 image does not know the knowledge class, so it cannot read a
  situation row recorded by this image that names it, until retention removes the row. The state file
  itself still opens. Back the state file up with the API stopped before the first start of a new image.
- **Index recovery.** If the `qdrant-data` volume is lost, nothing is lost: the next request rebuilds
  the collection from the files.

## Deferred

- A route that returns the body, `GET /context/knowledge/{id}/{version}`. It would be a new route and
  a new authority surface; consumers read the Markdown from the repository for now.
- Applicability that checks live graph or situation facts beyond the declared scope.
- Signed approval fields in the front matter, so that an approval can be verified apart from the
  commit that made it.
