# Performance and hygiene (Phase 8)

What Phase 8 changed in how the broker talks to its upstreams, how much it reads at once, what it
caches, how its dependencies and images are pinned, what the LodLive proxy accepts and how an operator
stops the simulator. This note describes Phase 8 as built, with the exact values the code uses; tests in
`tests/test_phase8_notes.py` fail when a default, a printed form, a refusal text or a name here differs
from the code.

The broker resolves, joins and delivers context. It never decides or acts. Nothing in this phase changes a
verdict, a reason, an eligibility result, a situation id or the eight keys of `/health`; the changes are in
speed, in what is bounded, in what is pinned and in what is named.

Code: `broker_engine/upstream_http.py`, `broker_engine/telemetry_reads.py`, `broker_engine/validation.py`,
`broker_engine/operations.py`, `intent_management/tmf921_source.py`, `intent_management/tmf921_client.py`,
`data_contracts/metric_map.py`, `data_contracts/telemetry.py`, `agent_harness/workflow.py`,
`lodlive-proxy/jsonp_proxy.py` and `stop_context.sh`. Tests: `tests/test_upstream_http.py`,
`tests/test_telemetry_reads.py`, `tests/test_intent_lock_extent.py`, `tests/test_phase8_settings.py`,
`tests/test_operations.py`, `tests/test_vm_parity.py`, `tests/test_metric_map.py`,
`tests/test_requirements_hashes.py`, `tests/test_dockerfile_pins.py`, `tests/test_lodlive_proxy.py`,
`tests/test_stop_context_script.py`, `tests/test_public_helpers.py` and, for this note,
`tests/test_phase8_notes.py`.

## Upstream sessions

Every call from `broker_engine` and `intent_management` to an upstream goes through
`broker_engine/upstream_http.py`. The upstreams are the three names in `UPSTREAMS`:

| Name | Upstream |
|---|---|
| `fuseki` | the network graph (SPARQL reads) |
| `victoriametrics` | telemetry (PromQL reads) |
| `tmf921` | the TMF921 intent server (reads, observation pushes) |

`get` and `post` take a name from that tuple and pass every keyword argument through unchanged; any other
name is a `ValueError`. One `requests.Session` per upstream is made on first use, under a lock, and kept for
the life of the process, so connections are reused (a pool of 2 hosts and 32 connections per host).

The rules a session follows:

- There is no retry. The adapter is built with `max_retries=0`, so the HTTP layer never repeats a request, and
  an observation push that fails once is sent once.

- There is no cookie. Every cookie a server sets is refused, so no later request carries one.
- No stored credential, header or timeout. The Fuseki credential is read from the environment on each
  call, so a rotated password is seen on the next request, and every call passes its own timeout.
- The operator scripts (the loader, the binder, the simulator and `seed_graph.py`) and the LodLive proxy stay
  on plain requests, and so does the Qdrant probe in `/health`; they do not share these sessions.

`reset_sessions` drops every session. Tests use it after each test; `tests/conftest.py` also routes the helper
to the patched `requests.get` and `requests.post` unless a test is marked `real_sessions`. A static test names
the file, line and function of any direct `requests` call left in these two packages.

## Parallel telemetry reads

The per-metric reads run on a pool made by `read_in_order` in `broker_engine/telemetry_reads.py`: one pool per
call, at most `BROKER_TELEMETRY_WORKERS` threads and never more than the number of metrics, results returned
in the order of the metrics, the first failure in that order raised. One metric or one worker runs inline. The
setting is read on each call, so the cap is per assess and per request, not process-wide.

Three paths read through it:

- `assess_via_tmf921` (the push-path assess), before the per-intent lock;
- `assess_tmf921_as_of` (the temporal assess), with the metrics first sorted into those that need a read and
  those that have a problem already, then read, then judged in metric order;
- the observations of a context request, with `recording()` made safe for concurrent callers: a key is read
  exactly once, concurrent callers for the same key take the stored envelope, and a read that raises stores
  nothing.

A metric's one to three joins stay on one worker. A refused interface identifier keeps its own slot in the
result. Equal results for one worker and for eight are a test for each path.

## What the per-intent lock covers

The lock around an assess that pushes holds only what must not interleave. A telemetry read no longer waits
behind another assess of the same intent.

Before the lock: the binding read, the state store availability read (the last outcome is read and the value
discarded, so a store failure still stops the assess before any TMF921 call), the goal check, the mapping check,
the telemetry reads and the pushability check. An early exit here carries its reasons out and is recorded
under the lock.

Inside the lock: the previous outcome (read again), the marks and regress check, the goal guard, the
freshness re-check, the report snapshot, the pushes (each followed by its mark), the poll, the success re-check
and the outcome write.

Why the goal guard and the freshness re-check are inside: an assess that waited for the lock may be looking at
a world that moved. The goal guard reads the intent again and stops a push against a goal that a concurrent
evaluation rewrote; without it the observation would be pushed against the wrong goal. The freshness re-check
refuses any observation older than the maximum age at that moment, so a value that went stale while waiting is
never pushed. Both use the same reasons and statuses as the checks before the lock. If the state store cannot
answer, the TMF921 assess fails closed before any call to the intent server.

## The series limit

`interfaces_with_nonzero` (`/context/interfaces-nonzero`) used to read every series of a device. Each of its
four rollups (`max_over_time`, `min_over_time`, `last_over_time`, `tlast_over_time`) now asks for at most the
limit plus one series, using the form held once in `operations.LIMITED_ROLLUP`:

```
limitk({n}, label_del({fn}({selector}), "__name__"))
```

with `n` equal to `BROKER_TSDB_SERIES_LIMIT` plus one. For the default and a device `r1` the maximum query is:

```
limitk(501, label_del(max_over_time(interface_metrics_input_drops{device="r1"}[5m]), "__name__"))
```

The metric name is deleted first because `limitk` ranks series by a hash of the full label set; without that,
`tlast_over_time` (which drops the name) would select different series from the other three. The result asks
for the limit plus one series so that one query both limits and detects a cut: if any rollup returns more than
the limit, the result was cut.

A cut keeps the series VictoriaMetrics ranks first by label hash, not the worst ones, so a cut result can omit
an interface with large drops. The broker takes the first N label sets in sorted order from the rows the engine
returned and uses the same ones in every rollup. The result says so:

- the output key `series_truncated` (always present, `false` when nothing was cut, also on the no-data path);
- the quality conflict `operations.SERIES_LIMIT_CONFLICT`, printed as `series limit reached ({limit})` with the
  validated limit, added first in `quality.conflicts`. A cut never makes `quality` look healthy.

`series_truncated` is separate from `truncated`, which still means that the output limit (1 to 200, `limit`)
cut the list of interfaces. Two series for one interface still give `multiple series for <interface>`.
`tests/test_vm_parity.py` checks the four forms against a real engine (see Supply chain, below).

## Caches

Two things are read from disk or parsed on every call in the past; both are cached now, and neither cache can
serve stale evidence or a failure.

- The metric map. `load_metric_map` keeps the parsed map under the key `metric_map_key` returns: the resolved path, the inode,
  the size and the modification time in nanoseconds, taken from one `stat` before the read. A changed file is
  read again. Only successful loads are stored, at most 16 entries; a missing file gives an empty map and a
  malformed file raises, each on every call. Callers get their own copy. `clear_metric_map_cache` empties it.
- The goal parse. `_parse_bounds` in `intent_management/tmf921_client.py` keeps the parsed goal of a Turtle
  expression under the text and the metric map key, at most 64 entries. A refusal that is deterministic is
  cached; an unreadable text raises and logs `intent goal unreadable` on every call. Callers get their own
  copy. `clear_bounds_cache` empties it. A five-metric call reads the map file once when cold and not at all
  when warm (five times before).

`tests/conftest.py` clears both caches before every test.

## Configuration

One variable per row, read on each call and checked at start. A bad value stops the API at start with a
message that names the variable and never echoes the value. Both are set in `docker-compose.yml` for
`contextbroker-api` only and listed, commented, in `.env.example`.

| Variable | Default | Limits |
|---|---|---|
| `BROKER_TELEMETRY_WORKERS` | `8` | whole number from 1 to 8; the telemetry reads of one assess or request |
| `BROKER_TSDB_SERIES_LIMIT` | `500` | whole number from 1 to 100000; the series one interfaces-nonzero rollup returns |

Two endpoint variables are read once at import by the operator scripts; the command-line `--endpoint` flags
still override them.

- `FUSEKI_DATA_ENDPOINT`, default `http://localhost:3030/network-dataset/data`: the loader
  (`ingestion/load_ontologies.py`), the binder (`ingestion/bind_intent.py`, which derives its update endpoint
  from it) and `seed_graph.py`.
- `VM_WRITE_ENDPOINT`, default `http://localhost:8428/write`: the simulator
  (`ingestion/telemetry_simulator.py`), whose write now carries the shared request timeout and raises instead
  of hanging.

The table about the state file is in `design/durable-state.md` and is unchanged.

## Supply chain

The API image installs only pinned, hashed requirements. The three lock files are generated; none is edited
by hand.

- `requirements.in` is the human list: 15 direct pins and the audit record (every pin is imported by non-test
  code or run by the Dockerfile; nothing was removed). `requirements.txt` is its lock, with every release
  file hash. `requirements-mcp.in` adds the optional MCP extra on top of it (`-c requirements.txt`), and
  `requirements-mcp.txt` is its lock; it holds exactly the same pins as the default lock plus the nine MCP
  packages. `lodlive-proxy/requirements.in` is constrained to the root lock the same way.
- pip-tools lives only in a throwaway virtual environment outside the repository, never in `venv/` and never
  in an image. Make one, run the three commands from the repository root, then delete it:

```bash
python3 -m venv "$SCRATCH/piptools" && "$SCRATCH/piptools/bin/pip" install pip-tools
pip-compile --generate-hashes --strip-extras --output-file=requirements.txt requirements.in
pip-compile --generate-hashes --strip-extras --output-file=requirements-mcp.txt requirements-mcp.in
pip-compile --generate-hashes --strip-extras --output-file=lodlive-proxy/requirements.txt lodlive-proxy/requirements.in
```

  Keep the versions already in a lock unless you mean to upgrade, and check that every pin equals what the
  tested environment runs.
- The images install with `--require-hashes --only-binary=:all:`, so a package whose hash does not match the
  lock is refused and no source distribution is built. The API Dockerfile and the proxy Dockerfile both
  install that way.
- Base images are pinned by digest, the index digest that covers every platform:
  - `python:3.14-slim` (API) = `sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f`
  - `python:3.13-slim` (proxy) = `sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90`
  - `maven:3.9-eclipse-temurin-21-jammy` = `sha256:7f8422bd81f19b9035007a75e2f82272b09af64db08848d7d4e93302466e8b6f`
  - `eclipse-temurin:21-jre-jammy` = `sha256:1eac8b8fee11ca7d6725a48bc5d41992ed1561504ebae25490d2d062847e8269`

  All four were read from the registry on 2026-11-11. A tag moves when it is rebuilt, so a digest is refreshed
  by hand, never automatically. To read one without a pull or a plugin, ask for a pull token, then send a HEAD
  request for the manifest with the index media types:

```bash
repo=library/python; tag=3.14-slim
tok=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:${repo}:pull" | sed -E 's/.*"token":"([^"]+)".*/\1/')
curl -sI -H "Authorization: Bearer $tok" -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json" \
  "https://registry-1.docker.io/v2/${repo}/manifests/${tag}" | tr -d '\r' | grep -i '^docker-content-digest'
```

  The value after `docker-content-digest:` goes after the `@` of the `FROM` line. `tests/test_dockerfile_pins.py`
  requires a digest on every `FROM` of the three Dockerfiles.
- The TMF921 image: `docker/tmf921/Dockerfile` now runs exactly `RUN mvn dependency:resolve -q`, with no pipe
  and no fallback, so a failing resolve fails the build. The image was not rebuilt in this phase.
- The three compose services pinned by tag only (VictoriaMetrics, Fuseki and Qdrant in `docker-compose.yml`)
  are bead `contextbroker-lln`.
- Two real-engine checks are skipped unless their URL is set, and refuse the persistent stores before any
  request. Both use a throwaway container with no volume, on a loopback port other than the persistent one:

```bash
docker run -d --rm --name phase8-vm -p 127.0.0.1:18428:8428 victoriametrics/victoria-metrics:v1.102.0
VM_PARITY_URL=http://127.0.0.1:18428 venv/bin/python -m pytest -q tests/test_vm_parity.py -rs
docker rm -f phase8-vm

docker run -d --rm --name parity-fuseki -p 127.0.0.1:3939:3030 secoresearch/fuseki:6.1.0 \
    java -cp '*:/javalibs/*' org.apache.jena.fuseki.main.cmds.FusekiServerCmd --update --mem /parity-dataset
FUSEKI_PARITY_URL=http://127.0.0.1:3939/parity-dataset venv/bin/python -m pytest -q tests/test_fuseki_parity.py
docker rm -f parity-fuseki
```

## File modes

`.env` is mode 600. Directories with group or other write permission are mode 755, outside `venv` and `.git`.
`.beads` stays 700. `.env` is only given a mode and is never read. Modes are not stored in git, and a directory
made later under a umask of 002 is 775 again, so the second command is run again after creating directories:

```bash
chmod 600 .env
find . \( -path ./venv -o -path ./.git \) -prune -o -type d -perm /022 -exec chmod 755 {} +
```

The same `find` with `-print | wc -l` instead of `-exec` counts what is left; it should print 0.

## The LodLive proxy

`lodlive-proxy/jsonp_proxy.py` forwards a query to Fuseki only when it is read-only. The check removes
comments, string literals and IRIs first, so a word inside them is not seen. Then the first word after any
`PREFIX` and `BASE` declarations must be one of the query forms and no word may be one of the refused ones.

- Query forms (`READ_FORMS`): `SELECT`, `ASK`, `DESCRIBE`, `CONSTRUCT`.
- Refused words (`FORBIDDEN_KEYWORDS`), anywhere in the query: `SERVICE`, `INSERT`, `DELETE`, `LOAD`, `CLEAR`,
  `CREATE`, `DROP`, `COPY`, `MOVE`, `ADD`, `WITH`, `USING`.
- `FROM` and `FROM NAMED` are allowed.
- A codepoint escape (a backslash and `u` or `U` followed by a hex digit) is refused outright: SPARQL reads
  such escapes before it reads words, so a refused word could hide behind one.
- An unterminated string is refused. A variable or prefixed name that only contains a refused word, such as
  `?service`, is not the word.

Every refusal is the same fixed 400 with the text `Only read-only SPARQL queries are accepted`
(`REFUSED_QUERY`), sent before any call to Fuseki and echoing nothing from the query. The other two replies
are fixed too: `Unsafe 'callback' parameter` (400, `UNSAFE_CALLBACK`) and `Upstream Fuseki request failed`
(502, `UPSTREAM_FAILED`, no exception text).

What this does not change: the proxy still adds the Fuseki credential to anonymous requests and still sends the
CORS header for any origin. That is bead `contextbroker-7i2`, which is open and is not part of Phase 8.

## Stopping the simulator

`./stop_context.sh [PIDFILE]` (the default is `telemetry.pid` in the current directory) stops the telemetry
simulator that the README starts in the background. It stops one process only: the process whose id is in the
pid file and whose command line names `telemetry_simulator`. It exits 0 after signalling that process and
removing the file. It exits 1 with one fixed line on stderr, and signals nothing, when the file is missing or
unreadable, holds anything but digits, or names a process that is not the simulator. It never touches a
container or the compose stack. The older instruction `kill $(cat telemetry.pid)` is gone.

## The agent's backend

`agent_harness.workflow.run_agent` returns an `AgentResult` with two fields: `answer` and `backend`, which is
`claude` or `lmstudio`. The backend is set at each return site (the normal path, the `LLM_BACKEND=lmstudio`
path and the fallback after Claude's API is unreachable), never from configuration or from a reply. The CLI
(`python broker.py "<question>"`) prints the answer on stdout, unchanged for a pipe, and `backend: <name>` on
stderr. A model that returned nothing gives an empty answer string.

## Public names

No non-test module imports another repository module's underscore name, and `tests/test_public_helpers.py`
finds any that does (its list of allowed exceptions is empty). The names that became public:

| Was | Is | Module |
|---|---|---|
| `_SAFE_TOKEN` | `SAFE_TOKEN` | `broker_engine/validation.py` |
| `_validate_token` | `validate_token` | `broker_engine/validation.py` |
| `_SAFE_WINDOW`, `_UNIT_SECONDS`, `_SAFE_RETENTION` | `SAFE_WINDOW`, `UNIT_SECONDS`, `SAFE_RETENTION` | `broker_engine/validation.py` |
| `_check_token` | `check_token` | `data_contracts/metric_map.py` |
| `_check_intent_id` | `check_intent_id` | `intent_management/tmf921_client.py` |
| `_short_name` | `short_name` | `outcome_assurance/tmf921_report.py` |

Two single homes came with them. `query_tsdb_metrics` in `broker_engine/tsdb_joiner.py` is the only function
behind `GET /context/metric`, the agent tool and the MCP tool, and it returns the full envelope, never a bare
number. `MEASUREMENT_NAME` is defined only in `data_contracts/telemetry.py`, and `broker_engine` imports
nothing from `ingestion`.

## Deferred

- One PromQL query for all the metrics of an intent, instead of one set of queries per metric.
- Automatic updates of the base image digests.
- Bead `contextbroker-7i2` (the proxy's credential and CORS header).
- Bead `contextbroker-lln` (the three compose images pinned by tag only).
