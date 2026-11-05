# Context Broker

## Commands
- `venv/bin/python -m pytest -q` - full suite (1642 pass, 26 skipped); live TMF921 tests need `TMF921_LIVE=1`, the real-engine parity tests need `FUSEKI_PARITY_URL`
- `python -m ingestion.bind_intent <intent_id> --place <Metric>=<device>/<interface> ...` - write a `:ServiceBinding` as goal and placement fluents in one SPARQL update; on an existing binding pass `--replace` (the goal changed: close the old goal, open the new) or `--correct` (the earlier record was wrong: keep it, mark it superseded), never both; `--replace --valid-from YYYY-MM-DDTHH:MM:SSZ` says when the goal really changed. Nothing is deleted, and `--replace` on an intent whose goal changed re-pins the changed goal (the original stays as a closed goal)
- `scripts/create_demo_intent.sh <ttl>` - create and activate a TMF921 demo intent (intents are never evaluated until ACTIVE)
- `docker start fuseki victoriametrics tmf921-server` - start the demo stack; `docker compose up -d contextbroker-api` then `docker network connect contextbroker-arch_default contextbroker-api` (lost on recreate, bead contextbroker-i2q). Never `docker compose down -v` (deletes Fuseki data)
- `tmf921-server` belongs to compose project `contextbroker-arch`; restart it with `docker restart tmf921-server`

## Auth and secrets
- Fuseki enforces basic auth on every endpoint, including `/$/ping`. `FUSEKI_ADMIN_PASSWORD` (letters and digits only) is required in `.env` by compose, and in the shell environment for `ingestion.load_ontologies`, `ingestion.bind_intent` and `seed_graph.py`. Never print or commit it.
- Every `/context/*` route needs a bearer JWT (RS256/ES256, `iss`/`aud` pinned, lifetime at most 3600 s): 401 if missing or invalid, 403 `{"detail": "insufficient scope"}` without `context:read` or `context:assess`. Mint with `scripts/mint_token.py` from a key kept outside the repo; the API gets only the public key (mode 644).
- `POST /context/request` also needs the signed `authority` claim (`entities`, `purposes`, `classes`), minted with `scripts/mint_token.py --entity/--purpose/--class` (all three or none): 403 `authority claims required` without it, 403 `outside authority scope: entity|purpose|class` when the request is outside it, all before any upstream call. Naming an `intent_id` also needs `context:assess` (403 `insufficient scope`). An empty, overlong or non-printable `sub` is 401 on every `/context/*` route.
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
- `design/` - tracked analysis docs (e.g. `layer3-gap-analysis.md`, `context-contract.md` for the Phase 3 request and response contract); `docs/` is untracked, so anything meant for git goes here

## Gotchas
- TMF921 server evaluates latency, jitter and packet loss with strict `<`; bandwidth with `>=` (`data_contracts/metric_map.json`).
- After a Degraded evaluation the server replaces the failed goal with the observed value (Flow 3) and keeps no original; the goal pinned in the graph (a goal fluent with its valid and recorded times) is the only record.
- Fluent times are `:validStart`/`:validEnd` (not `:validFrom`/`:validTo`) because migration 001 deletes every triple with those two predicates. `assess` and `neighbours` take `as_of` and `known_at` offsets, limited by `BROKER_RETENTION_PERIOD` (default `30d`, digits then `h` or `d`).
- ABox files load only with the `# written by ingestion.bind_intent` first line or by the static allow-list (`topology-demo.ttl`). `ontologies/abox/topology-demo.ttl` is tracked through a `.gitignore` negation and is for fresh stores only; a store from before Phase 2 needs `ontologies/migrations/002-bitemporal-fluents.ru` (one-way, run only after the operator's go-ahead; runbook in `design/bitemporal-fluents.md`).
- `tests/test_fuseki_parity.py` needs `FUSEKI_PARITY_URL` (a throwaway dataset on 127.0.0.1 or localhost) and refuses port 3030 and `network-dataset`; never point it at the persistent store.
- Fuseki needs host port 3030; another project's container (BBF_921_java) may hold it.
- Every envelope's `provenance` is a list of four-field records (source system, reference, observation time, retrieval time); an empty one is `[]`, never `{}`.
- Situation ids (`sit-` plus 32 hex) are pinned by a known-answer test because Phase 4 stores them; changing their inputs, or the node id rules (`Router-A`, `Router-A/GigabitEthernet0/0/0`), needs a `SITUATION_VERSION` bump.
- `tests/test_bind_intent.py` assumes the query endpoint URL ends in `/query`.


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
