# Context Broker

## Commands
- `venv/bin/python -m pytest -q` - full suite (3379 pass, 28 skipped); live TMF921 tests need `TMF921_LIVE=1`, the real-engine parity tests need `FUSEKI_PARITY_URL`
- `python -m ingestion.bind_intent <intent_id> --place <Metric>=<device>/<interface> ...` - write a `:ServiceBinding` as goal and placement fluents in one SPARQL update; on an existing binding pass `--replace` (the goal changed: close the old goal, open the new) or `--correct` (the earlier record was wrong: keep it, mark it superseded), never both; `--replace --valid-from YYYY-MM-DDTHH:MM:SSZ` says when the goal really changed. Nothing is deleted, and `--replace` on an intent whose goal changed re-pins the changed goal (the original stays as a closed goal)
- `docker exec -i contextbroker-api python -m ingestion.syslog_ingest < file` - store syslog lines (RFC 5424 and RFC 3164) as events in the state file; reads the device names from Fuseki first and stores nothing if Fuseki cannot answer; prints one summary line (stored, duplicates, unmapped, ambiguous, quarantined per reason). No listener and no ingest route
- `docker exec contextbroker-api python -m ingestion.promote --situation sit-... [--dry-run]` - keep what a stored events situation shows as promoted records: a conflict (repeated `link_down` events against a configured link) and a hypothesis (a link that stored neighbour-discovery lines report and the graph lacks). The only writer of the `promoted` table; reads Fuseki with SELECT only; prints one line of counts; every refusal is one fixed line with nothing written (see `design/semantic-promotion.md`)
- `./stop_context.sh [PIDFILE]` - stop the telemetry simulator named in `telemetry.pid`; it signals only a process whose command line names `telemetry_simulator` and never touches a container (exit 1 with one fixed line otherwise)
- `scripts/create_demo_intent.sh <ttl>` - create and activate a TMF921 demo intent (intents are never evaluated until ACTIVE)
- `docker start fuseki victoriametrics qdrant tmf921-server` - start the demo stack; `docker compose up -d contextbroker-api` then `docker network connect contextbroker-arch_default contextbroker-api` (lost on recreate, bead contextbroker-i2q). Never `docker compose down -v` (deletes Fuseki data)
- `tmf921-server` belongs to compose project `contextbroker-arch`; restart it with `docker restart tmf921-server`

## Auth and secrets
- Fuseki enforces basic auth on every data route (`/$/ping` alone answers without one: liveness only), with two users (`docker/fuseki/config.ttl`): `admin` (query, update, graph store) and `reader` (query and ping only). `FUSEKI_ADMIN_PASSWORD` and `FUSEKI_READER_PASSWORD` (letters and digits only, different) are both required in `.env` by compose. The API and the LodLive proxy get only the reader one, so they cannot update the graph. The shell environment needs the admin one for `ingestion.load_ontologies`, `ingestion.bind_intent` and `seed_graph.py` (run `bind_intent` on the host). `scripts/check_fuseki_reader.sh` proves the reader is refused on update. Never print or commit either.
- `QDRANT_API_KEY` (letters and digits only) is required in `.env` by compose, for every compose command; the API reads the same variable. Qdrant is published on `127.0.0.1:6333` only. Never print or commit it.
- Every `/context/*` route needs a bearer JWT (RS256/ES256, `iss`/`aud` pinned, lifetime at most 3600 s): 401 if missing or invalid, 403 `{"detail": "insufficient scope"}` without `context:read` or `context:assess`. Mint with `scripts/mint_token.py` from a key kept outside the repo; the API gets only the public key (mode 644).
- Every `/context/*` route also needs the signed `authority` claim (`entities`, `purposes`, `classes`; the classes are `topology`, `goals`, `observations`, `events` and `knowledge`), minted with `scripts/mint_token.py --entity/--purpose/--class` (all three or none): 403 `authority claims required` without it, 403 `outside authority scope: entity|purpose|class` when the request is outside it, all before any upstream call. The raw routes test the `device` and one class (`observations` for `/context/metric`, `/metric-history` and `/interfaces-nonzero`, `topology` for `/neighbours`) and no purpose. `POST /context/assess` needs `goals` and `observations` and every device the intent is placed on in `entities`, tested once the binding is read and before any TMF921 call or telemetry read (in-process callers pass no authority). Naming an `intent_id` also needs `context:assess` (403 `insufficient scope`). An empty, overlong or non-printable `sub` is 401 on every `/context/*` route.
- `GET /context/situation/{id}` needs `context:read` and the authority claims, returns a stored situation only to its original requester (anyone else gets the same 404 `situation not found` as for an unknown id) and needs `context:assess` when the situation names an intent. Every answered `POST /context/request` records one situation row; a refusal records nothing.
- `GET /health` is unauthenticated and reports each upstream with fixed strings (200, or 503). The API container reports unhealthy without the tmf921 profile.
- The graph intent path is gone (`source` is `tmf921` only; no `/context/intent/{id}`; no `:OperationalIntent`).

## Branches
- Work on `fullnetwork` (pushed to `origin/fullnetwork`). Do not push `origin/master` unless asked.

## Licensing (never break)
- TM Forum TIO ontology files and the TMF921/TR290-292 PDFs are licensed: never commit, push, copy content into the repo, or bake into an image. The licensed material is `docs/TMF-921-spec/` only. `docs/` is still deliberately untracked.
- Do not modify `/home/brad/Projects/BBF_921_java` (the TMF921 server source). The broker never creates, patches or deletes a TMF921 intent.

## Architecture rules
- The broker resolves, joins and delivers context; it never decides or acts. Verdicts are `success|failure|inconclusive`; stale, missing, conflicted, changed-goal or non-finite evidence is never `success`.
- Two separate Jena instances: Fuseki (network graph, port 3030, persistent TDB2) and the TMF921 server's embedded Jena. Fuseki and lodlive-proxy stay bound to 127.0.0.1.
- Reasons are fixed strings built only from pinned metric names and finite numbers; never echo server text.
- Context eligibility (`eligible|ineligible|gap`, per requested class) is separate from the verdict: it never feeds it, the two can disagree, and it grants no execution authority. A context request never pushes, writes a high-water mark or records an outcome.

## Wording
- Say "goal" (one metric with its limit), not "bound"; avoid "drift". The stored `:pinnedBound` and internal `intent_bounds` names stay as they are.
- "Fails closed" must name its subject (for example "the TMF921 assess fails closed"). Do not call the action-readiness check a "gate".

## Docs map (untracked; only `docs/TMF-921-spec/` is licensed, read only)
- `docs/Architecture/` - functional architecture spec with implementation options; Layer 3 (Context Broker, E01-E03) is the layer this repo implements
- `docs/AI_Ready_Network_Operations_Layer_Markdowns/` - one file per layer (`03_...` is Semantic Knowledge and Context), plus `08_Cross_Cutting_Fabrics.md`
- `docs/TMF-921-spec/` - LICENSED. TMF921 intent interface: markdown `01`-`11`, OAS yaml, PDFs
- `docs/superpowers/plans/` - dated implementation plans

## Design notes (tracked)
- `design/` - tracked analysis docs (e.g. `exchange-profile.md` for the JSON-LD profile and its `@context`, `semantic-promotion.md` for promoted conflicts, hypotheses and the operator command, `layer3-gap-analysis.md`, `context-contract.md` for the Phase 3 request and response contract, `durable-state.md` for what the broker stores, retention, eviction and the store's failure rules, `event-store.md` for events: storage, ingest, the events class, reconciliation and the state-vocabulary mapping, `knowledge.md` for governed knowledge: the Qdrant decision, bundles and approval, applicability, the knowledge class and its operation, `performance-and-hygiene.md` for Phase 8: sessions, parallel reads, the lock extent, the series limit, caches, the hashed locks and digests, file modes, the proxy filter and the public names); `docs/` is untracked, so anything meant for git goes here

## Gotchas
- State lives in one SQLite file on the `broker-state` volume at `/data` (`BROKER_STATE_PATH`, never `:memory:`): last outcome per intent, high-water marks and situation records. The API refuses to start without it and answers 503 `state store unavailable` when it fails mid-request; there is no memory fallback. Caps and retention: `BROKER_OUTCOME_CAP`, `BROKER_MARK_CAP`, `BROKER_SITUATION_CAP`, `BROKER_SITUATION_RETENTION` (see `design/durable-state.md`). One API worker is the supported setup. Never `docker compose down -v`: it deletes `broker-state`.
- Library-only packages (`governance_gate`, `action_lifecycle`, `decision_records`, `investigation_workers`, `loop_coordination`; `platform_services` and `source_federation` are empty) stay in the repo with their tests but are listed in `.dockerignore`, so the image does not carry them, and no runtime path imports them (`tests/test_unwired_packages.py` pins both). Decision records are a file by default (`BROKER_DECISION_RECORDS_PATH`, append-only, never swept; compose does not set it because no running service opens one); `RecordStore(":memory:")` only when a caller asks. The reservation book stays in memory. Tests get a per-test state file and decision-record file from `tests/conftest.py`.
- TMF921 server evaluates latency, jitter and packet loss with strict `<`; bandwidth with `>=` (`data_contracts/metric_map.json`).
- After a Degraded evaluation the server replaces the failed goal with the observed value (Flow 3) and keeps no original; the goal pinned in the graph (a goal fluent with its valid and recorded times) is the only record.
- Fluent times are `:validStart`/`:validEnd` (not `:validFrom`/`:validTo`) because migration 001 deletes every triple with those two predicates. `assess` and `neighbours` take `as_of` and `known_at` offsets, limited by `BROKER_RETENTION_PERIOD` (default `30d`, digits then `h` or `d`).
- ABox files load only with the `# written by ingestion.bind_intent` first line or by the static allow-list (`topology-demo.ttl`). `ontologies/abox/topology-demo.ttl` is tracked through a `.gitignore` negation and is for fresh stores only; a store from before Phase 2 needs `ontologies/migrations/002-bitemporal-fluents.ru` (one-way, run only after the operator's go-ahead; runbook in `design/bitemporal-fluents.md`).
- `tests/test_fuseki_parity.py` needs `FUSEKI_PARITY_URL` (a throwaway dataset on 127.0.0.1 or localhost) and refuses port 3030 and `network-dataset`; never point it at the persistent store.
- Fuseki needs host port 3030; another project's container (BBF_921_java) may hold it.
- Every envelope's `provenance` is a list of four-field records (source system, reference, observation time, retrieval time); an empty one is `[]`, never `{}`.
- Situation ids (`sit-` plus 32 hex) are pinned by a known-answer test because Phase 4 stores them; changing their inputs, or the node id rules (`Router-A`, `Router-A/GigabitEthernet0/0/0`), needs a `SITUATION_VERSION` bump.
- `tests/test_bind_intent.py` assumes the query endpoint URL ends in `/query`.
- Events live in the state file too (`events` and `event_devices` tables; `BROKER_EVENT_CAP`, `BROKER_EVENT_RETENTION`, `BROKER_EVENT_WINDOW_DEFAULT`, `BROKER_EVENT_WINDOW_MAX`). Quarantined text is never kept, and raw or message text is never returned by any route. Schema policy is additive: `user_version` stays 1, so rollback is an image rollback (back up the file with the API stopped before the first start of a new image). `/health` has eight keys (`events` and `evictions.events` came with events; `qdrant` with knowledge). Syslog origin is unauthenticated, so a mapped event is a claim by whoever sent it. The reconciler never writes configured state and never ranks the two sides; a conflict never reaches the verdict or eligibility. `window_seconds` joins the situation id only for an events request, so `SITUATION_VERSION` stays 1.
- Knowledge: bundles are tracked in `knowledge/` and mounted read-only; approval is a reviewed commit and the broker never writes a status. The Qdrant index is derived data, rebuilt from the files and holding only identity and vectors. The two embedding models are baked into the image by commit with `HF_HUB_OFFLINE=1`. A response carries references only, never a body, excerpt or ranking value. Knowledge is current only (`as_of` or `known_at` gives `not_current`), and when retrieval cannot answer the knowledge branch is a gap while the other classes and the verdict are unchanged. `topic` joins the situation id only when set. The state file gains no table. See `design/knowledge.md`.
- JSON-LD exchange profile: `Accept: application/ld+json` on `POST /context/request` only; there is no new route and no `/context/ld`. The header never refuses (an unusable one is native JSON, 200, `Vary: Accept`), errors stay `application/json`, and the situation id and stored row are unaffected. The `@context` is inline and static and printed in `design/exchange-profile.md`, where a test pins it equal to the code. The profile adds no field of its own. See `design/exchange-profile.md`.
- MCP server: built and unit-tested, not served, and an optional extra. `requirements-mcp.txt` holds the nine MCP-stack pins after `-r requirements.txt`; the image installs `requirements.txt` only, and the MCP tests skip without the package (`importorskip`).
- Promoted records: the `promoted` table is in the state file (additive, `user_version` stays 1; back up the file with the API stopped before the first start of the Phase 7 image). It is written only by `ingestion.promote`, never by a request, an ingest or a timer. The eviction count is `evicted_promoted` in `counters`, so `/health` still has eight keys. A hypothesis is a separate list (`graph.hypotheses`) labelled `hypothesis` and `inferred`, and no promoted record is ever evidence: it feeds no verdict, eligibility or expiry and changes no configured state. Raw and peer text is never stored, only graph names and ids. Syslog origin is unauthenticated, so a promoted record is a claim by whoever sent the lines. One neighbour-discovery wording (`lldp_neighbor_up`) is proposed and unverified against a device. See `design/semantic-promotion.md`.
- Upstream sessions: `broker_engine/upstream_http.py` holds one reused `requests.Session` per upstream (`fuseki`, `victoriametrics`, `tmf921`), with no retry, no cookie and the credential read on each call. `tests/conftest.py` routes it to the patched `requests.get` and `requests.post` unless a test is marked `real_sessions`; a new test that needs a real socket uses that marker. The operator scripts, the proxy and the Qdrant probe stay on plain requests.
- Parallel reads: the telemetry reads of an assess or a request run on at most `BROKER_TELEMETRY_WORKERS` threads (default 8, 1 to 8), results in metric order. The per-intent lock holds only the marks, goal guard, freshness re-check, snapshot, pushes, poll, success re-check and outcome write. `BROKER_TSDB_SERIES_LIMIT` (default 500, 1 to 100000) caps the series of one `interfaces-nonzero` rollup; `series_truncated` and `series limit reached (N)` say a cut happened, and the cut keeps the series VictoriaMetrics ranks first by label hash, not the worst ones. `tests/test_vm_parity.py` needs `VM_PARITY_URL` (a throwaway VictoriaMetrics on loopback; it refuses port 8428). The metric map and the parsed goal are cached (`clear_metric_map_cache`, `clear_bounds_cache`; `conftest.py` clears both). `FUSEKI_DATA_ENDPOINT` and `VM_WRITE_ENDPOINT` set the operator scripts' write endpoints.
- Dependencies: `requirements.txt`, `requirements-mcp.txt` and `lodlive-proxy/requirements.txt` are hashed locks generated from their `.in` files (`pip-compile --generate-hashes --strip-extras --output-file=requirements.txt requirements.in`); never edit one by hand. Run pip-tools only in a throwaway virtual environment outside the repository, never in `venv/` or an image. Images install with `--require-hashes --only-binary=:all:` and every `FROM` carries a digest (refresh by hand; the read is in `design/performance-and-hygiene.md`). The three compose images pinned by tag only are bead `contextbroker-lln`.
- LodLive proxy: it forwards only read-only SPARQL (a fixed 400 for `SERVICE`, updates and codepoint escapes; `FROM` and `FROM NAMED` allowed). It holds the reader credential only, sends no CORS header and answers only a request whose `Origin` or `Referer` is the LodLive page (`LODLIVE_ALLOWED_ORIGINS`, default `http://localhost:8888` and `http://127.0.0.1:8888`), else a fixed 403.
- Agent: `run_agent` returns `AgentResult(answer, backend)`; the CLI prints the answer on stdout and `backend: claude` or `backend: lmstudio` on stderr.
- File modes: `.env` is 600, directories with group or other write are 755 (outside `venv` and `.git`), `.beads` stays 700. Git does not store modes, so rerun `find . \( -path ./venv -o -path ./.git \) -prune -o -type d -perm /022 -exec chmod 755 {} +` after creating directories.


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## GSD + Beads

GSD (`/gsd-*` skills, open-gsd/gsd-core) plans the work and beads tracks it. They are not integrated, so keep the roles separate.

### Roles
- **GSD owns the plan:** `.planning/` holds PROJECT, REQUIREMENTS, ROADMAP, phase plans and verification reports.
- **Beads owns tracking:** every piece of work has a bead (`bd`). Status, claiming, dependencies and closing live there only.
- **No second tracker:** `.planning/STATE.md` and checklists in plan files are plan content, not progress. Do not mark work done there instead of in beads.

### Workflow per phase
1. `bd ready`, and check the phase is not already covered by an open bead.
2. Create or reuse one epic per GSD phase, with one bead per plan task:
   `bd create --title="Phase N: <name>" --type=feature --description="..."`
   Put the plan path (`.planning/phases/...`) in the bead description or `--notes`.
3. `bd update <id> --claim` before `/gsd-execute-phase` (or `/gsd-quick`).
4. Run `/gsd-discuss-phase`, `/gsd-plan-phase`, `/gsd-execute-phase` and `/gsd-verify-work` as needed.
5. `bd close <id> --reason="..."` when verified. File follow-ups with `bd create` and link them with `bd dep add`.
6. Run the session close protocol (Session Completion, below).

### Rules
- Create the bead before running a GSD execute step, not after.
- Work that GSD discovers but does not do (review findings, deferred items, UAT failures) becomes a bead. Do not leave it only in a GSD report.
- Persistent knowledge goes in `bd remember`. Do not use the `gsd-mempalace-*` skills or MEMORY.md files.
- GSD executors commit atomically. Keep them on `fullnetwork`, and do not push `origin/master`.
- `.planning/` is gitignored: it is derived planning state and may hold licensed text. Do not `git add -f` it. Anything meant for git goes in `design/`.

### Licensing (never break)
- Do not point GSD at `docs/TMF-921-spec/` (`/gsd-onboard`, `/gsd-ingest-docs`, `/gsd-map-codebase`). It is licensed, read-only and untracked. The other `docs/` subfolders (`Architecture/`, `AI_Ready_Network_Operations_Layer_Markdowns/`, `superpowers/`) are not licensed and may be used as GSD input.
- Nothing from `docs/TMF-921-spec/` may be copied into `.planning/`, beads descriptions or `design/`.

### Architecture rules still apply
- GSD plans must keep the broker's rules from this file: it resolves, joins and delivers context and never decides or acts. Verdicts stay `success|failure|inconclusive`.
- Use "goal", not "bound", in plans and bead titles.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
