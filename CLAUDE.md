# Context Broker

## Commands
- `venv/bin/python -m pytest -q` - full suite (716 pass, 15 skipped); live TMF921 tests need `TMF921_LIVE=1`
- `python -m ingestion.bind_intent <intent_id> --place <Metric>=<device>/<interface> ...` - write a `:ServiceBinding`; `--replace` on an intent whose goal changed re-pins the changed goal
- `scripts/create_demo_intent.sh <ttl>` - create and activate a TMF921 demo intent (intents are never evaluated until ACTIVE)
- `docker start fuseki victoriametrics tmf921-server` - start the demo stack. Never `docker compose down -v` (deletes Fuseki data)
- `tmf921-server` belongs to compose project `contextbroker-arch`; restart it with `docker restart tmf921-server`

## Branches
- Work on `fullnetwork` (pushed to `origin/fullnetwork`). Do not push `origin/master` unless asked.

## Licensing (never break)
- TM Forum TIO ontology files and the TMF921/TR290-292 PDFs are licensed: never commit, push, copy content into the repo, or bake into an image. `docs/` is deliberately untracked.
- Do not modify `/home/brad/Projects/BBF_921_java` (the TMF921 server source). The broker never creates, patches or deletes a TMF921 intent.

## Architecture rules
- The broker resolves, joins and delivers context; it never decides or acts. Verdicts are `success|failure|inconclusive`; stale, missing, conflicted, changed-goal or non-finite evidence is never `success`.
- Two separate Jena instances: Fuseki (network graph, port 3030, persistent TDB2) and the TMF921 server's embedded Jena. Fuseki and lodlive-proxy stay bound to 127.0.0.1.
- Reasons are fixed strings built only from pinned metric names and finite numbers; never echo server text.

## Wording
- Say "goal" (one metric with its limit), not "bound"; avoid "drift". The stored `:pinnedBound` and internal `intent_bounds` names stay as they are.
- "Fails closed" must name its subject (for example "the TMF921 assess fails closed"). Do not call the action-readiness check a "gate".

## Docs map (untracked, licensed material: read only)
- `docs/Architecture/` - functional architecture spec with implementation options; Layer 3 (Context Broker, E01-E03) is the layer this repo implements
- `docs/AI_Ready_Network_Operations_Layer_Markdowns/` - one file per layer (`03_...` is Semantic Knowledge and Context), plus `08_Cross_Cutting_Fabrics.md`
- `docs/TMF-921-spec/` - TMF921 intent interface: markdown `01`-`11`, OAS yaml, PDFs
- `docs/superpowers/plans/` - dated implementation plans

## Design notes (tracked)
- `design/` - tracked analysis docs (e.g. `layer3-gap-analysis.md`); `docs/` is untracked, so anything meant for git goes here

## Gotchas
- TMF921 server evaluates latency, jitter and packet loss with strict `<`; bandwidth with `>=` (`data_contracts/metric_map.json`).
- After a Degraded evaluation the server replaces the failed goal with the observed value (Flow 3) and keeps no original; the goal pinned in the graph is the only record.
- Fuseki needs host port 3030; another project's container (BBF_921_java) may hold it.
- `tests/test_bind_intent.py` assumes the query endpoint URL ends in `/query`.
