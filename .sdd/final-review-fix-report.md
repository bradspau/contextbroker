# Final Review Fix Report

Cross-cutting fix pass on top of the 5 already-approved implementation tasks. All 6
findings from the whole-project review addressed below. No git operations were
performed (project does not use git).

Final full-suite run: **25/25 passed** (`venv/bin/pytest tests/ -v`).

---

## Finding 1 (Critical): Fuseki image does not exist

### Evidence gathered

- `https://hub.docker.com/r/apache/jena-fuseki` → **HTTP 404 Not Found**. Confirms the
  image referenced in the original `docker-compose.yml`
  (`apache/jena-fuseki:5.1.0`) does not exist on Docker Hub.
- `https://hub.docker.com/r/stain/jena-fuseki` → exists, description: "Apache Jena
  Fuseki 2: SPARQL 1.1 server with web UI, backed by Apache Jena's TDB triple store."
- Docker Hub API (`https://hub.docker.com/v2/repositories/stain/jena-fuseki/tags?page_size=100`)
  confirms a tag named **`5.1.0`** exists, with the **same digest**
  (`sha256:b1d0c96f19adddef417520184471ac816cf36c6e72936a88ea02b29f4305929a`) as `latest`,
  pushed 2024-07-31. Chosen so the version string in compose stays `5.1.0`, minimizing
  the diff.
- GitHub source, `https://github.com/stain/jena-docker` →
  `jena-fuseki/Dockerfile` (fetched via
  `https://raw.githubusercontent.com/stain/jena-docker/master/jena-fuseki/Dockerfile`):
  ```
  ENTRYPOINT ["/sbin/tini", "--", "sh", "/docker-entrypoint.sh"]
  CMD ["/jena-fuseki/fuseki-server"]
  ```
  `FUSEKI_HOME=/jena-fuseki`, `EXPOSE 3030`.
- `jena-fuseki/docker-entrypoint.sh` (fetched via
  `https://raw.githubusercontent.com/stain/jena-docker/master/jena-fuseki/docker-entrypoint.sh`):
  sets up `shiro.ini`/admin password, then does `exec "$@" &` — i.e. it execs whatever
  args follow the entrypoint (the effective `CMD`, or compose's `command:` override)
  literally as a process invocation, **not** as flags appended to a fixed binary.

### Why the original command was broken

Docker Compose's `command:` fully replaces the image's `CMD` (it does not append to
it). The default `CMD` is `["/jena-fuseki/fuseki-server"]`; overriding `command:` to
`["--update", "--mem", "/network-dataset"]` means the entrypoint script runs
`exec "--update" "--mem" "/network-dataset" &`, which tries to execute a program
literally named `--update` — this fails (`exec: "--update": no such file or directory`),
so the container would never actually run.

### Fix

`docker-compose.yml`:
```diff
  fuseki:
-   image: apache/jena-fuseki:5.1.0
+   image: stain/jena-fuseki:5.1.0
    container_name: fuseki
    ports:
      - "3030:3030"
-   command: ["--update", "--mem", "/network-dataset"]
+   command: ["/jena-fuseki/fuseki-server", "--update", "--mem", "/network-dataset"]
    restart: unless-stopped
```
Port mapping (`3030:3030`) and dataset name (`/network-dataset`) unchanged, as required.

### Verification

```
$ venv/bin/pytest tests/test_docker_compose.py -v
tests/test_docker_compose.py::test_docker_compose_defines_victoriametrics_and_fuseki PASSED
1 passed in 0.03s
```
Docker itself is unavailable in this sandbox (no `docker` binary), so this was verified
by source inspection of the real Dockerfile/entrypoint script rather than by running
`docker compose up`/`pull`, per the task's constraints.

---

## Finding 2 (Important): Telemetry simulator default drop rate breaks the demo

### Before
```python
def generate_metrics(rng: random.Random) -> dict:
    return {
        "in_octets": rng.randint(1_000_000, 5_000_000),
        "out_octets": rng.randint(1_000_000, 5_000_000),
        "input_drops": 0 if rng.random() > 0.05 else rng.randint(1, 20),
    }

def build_payload(rng: random.Random) -> str: ...
def run(write_endpoint=WRITE_ENDPOINT, once=False) -> None: ...
```

### After
- `generate_metrics(rng, inject_drops: bool = False)` — `input_drops` is always `0`
  unless `inject_drops=True`, in which case the original 5%-chance logic runs
  unchanged.
- `build_payload(rng, inject_drops: bool = False)` threads the flag into
  `generate_metrics`.
- `run(write_endpoint=WRITE_ENDPOINT, once=False, inject_drops: bool = False)` threads
  it into `build_payload`.
- New CLI flag `--inject-drops` (`action="store_true"`), passed to `run()`.

### Verification

New tests in `tests/test_telemetry_simulator.py`:
- `test_generate_metrics_defaults_to_no_drops` — 200 different seeds, both with no
  argument and with explicit `inject_drops=False`, always `input_drops == 0`.
- `test_generate_metrics_can_inject_drops_when_requested` — 200 different seeds with
  `inject_drops=True`; asserts both a zero and a nonzero `input_drops` value are
  observed across the trials (no exact-count assertion, per instructions).

```
$ venv/bin/pytest tests/test_telemetry_simulator.py -v
6 passed in 0.10s
```
(4 original + 2 new; the original `test_generate_metrics_returns_nonnegative_integers`
also still passes unchanged since it doesn't rely on drop behavior.)

---

## Finding 3 (Important) + Finding 4 (Important): SSRF via LLM tool kwargs / unhandled tool exceptions

These were implemented together in `broker.py`'s `run_agent`, as instructed.

### Finding 3 fix — schema-derived kwarg allow-list

Added, right after the `TOOLS` list (so it's derived from the single source of truth,
not a second hand-typed copy):
```python
_TOOL_ALLOWED_ARGS = {
    tool["function"]["name"]: set(tool["function"]["parameters"]["properties"])
    for tool in TOOLS
}
```
Inside the per-tool-call dispatch, before calling the function:
```python
allowed_keys = _TOOL_ALLOWED_ARGS[name]
extra_keys = set(arguments) - allowed_keys
if extra_keys:
    raise ValueError(f"Unexpected argument(s) for tool {name!r}: {sorted(extra_keys)}")
```
This rejects e.g. an LLM-supplied `query_endpoint` on either tool (neither tool's
declared JSON schema includes it), preventing the SSRF redirect described in the
finding.

### Finding 4 fix — catch, feed back, and bound the loop

- Added `MAX_ITERATIONS = 10` as a module constant near the other constants.
- Changed `while True:` to `for _ in range(MAX_ITERATIONS):`.
- Wrapped the per-tool-call block (name allow-list check, kwarg allow-list check, and
  the actual `func(**arguments)` call) in `try/except Exception as exc`. On failure,
  instead of raising, a `role: "tool"` message with the matching `tool_call_id` and
  `content=json.dumps({"error": str(exc)})` is appended, and the loop continues so the
  model can see the error on its next `create()` call.
- If the `for` loop exhausts without a non-tool-call message being returned, the
  function now returns
  `f"Unable to reach a final answer after {MAX_ITERATIONS} tool-calling iterations."`
  instead of falling through (previously `while True:` had no fallthrough path at all).

### Verification

New tests in `tests/test_broker.py`:
- `test_run_agent_rejects_llm_supplied_extra_kwarg_and_recovers` — mocked tool call for
  `query_intent_state_requirements` with an extra `query_endpoint` key aimed at
  `http://attacker.example/x`. Asserts the real (patched) tool function is
  **never called** (`mock_tool.assert_not_called()`), that `run_agent` returns the
  model's second-turn final message instead of raising, and that the appended
  message for the failed call has `role: "tool"` with `content` containing
  `{"error": "...query_endpoint..."}`.
- `test_run_agent_recovers_from_unsafe_tool_argument_value` — mocked call to
  `query_tsdb_metrics` with `time_window="5min"` (rejected by `_SAFE_WINDOW`/
  `_validate_token` inside the real function). Asserts recovery via the same
  error-feedback mechanism.
- `test_run_agent_rejects_unknown_tool_name_without_raising` — unknown tool name
  (`delete_all_data`), asserts recovery without raising.
- `test_run_agent_stops_after_max_iterations_without_raising` — a mock client that
  *always* returns a tool call (never a final message). Asserts `run_agent` returns
  `"Unable to reach a final answer after 10 tool-calling iterations."` and that
  `create.call_count <= broker.MAX_ITERATIONS` (it is exactly `MAX_ITERATIONS` given
  `for _ in range(MAX_ITERATIONS)`).

```
$ venv/bin/pytest tests/test_broker.py -v
15 passed in 1.25s
```
(9 original + 6 new: the 4 above plus the Finding 5 and Finding 6 tests below.)

---

## Finding 5 (Important): Duplicated "interface_metrics" literal

### Fix
- `telemetry_simulator.py`: added `MEASUREMENT_NAME = "interface_metrics"` as a
  module constant; `format_line_protocol` now interpolates
  `f"{MEASUREMENT_NAME},{tags} ..."` instead of the literal string.
- `broker.py`: added `from telemetry_simulator import MEASUREMENT_NAME` at the top,
  and `query_tsdb_metrics`'s PromQL construction now uses
  `f'max_over_time({MEASUREMENT_NAME}_{metric_type}...'` instead of the hardcoded
  `interface_metrics_{metric_type}`.

### Verification
New test `test_measurement_name_constant_matches_telemetry_simulator` in
`tests/test_broker.py`:
```python
assert broker.MEASUREMENT_NAME == telemetry_simulator.MEASUREMENT_NAME == "interface_metrics"
```
This passes because the `from ... import MEASUREMENT_NAME` statement binds the same
string object into `broker`'s module namespace — the two names cannot silently
diverge without a code change on both sides.

Full suite run to confirm the new intra-project import doesn't cause a circular-import
problem (it doesn't — `telemetry_simulator.py` has no dependency on `broker.py`):
```
$ venv/bin/pytest tests/ -v
25 passed in 1.25s
```

---

## Finding 6 (Important): No test locks the SPARQL-vs-ontology contract

### Fix
Extracted the SPARQL string construction out of `query_intent_state_requirements`
into its own function:
```python
def _build_intent_sparql(intent_id: str) -> str:
    _validate_token(intent_id, "intent_id")
    return f"""
    PREFIX : <http://example.org/network-ontology#>
    SELECT ?metric ?operator ?targetValue ?device ?interfaceName WHERE {{
      :{intent_id} :hasTargetMetric ?metric ;
                    :hasOperator ?operator ;
                    :hasTargetValue ?targetValue ;
                    :appliesToLink ?link .
      ?link :hasInterface ?iface .
      ?iface :onDevice ?device ;
             :interfaceName ?interfaceName .
    }}
    """
```
`query_intent_state_requirements` now calls this internally and is otherwise
unchanged (still does the identical POST with the identical query text) — pure
refactor, no behavior change (confirmed by the pre-existing
`test_query_intent_state_requirements_parses_sparql_json` and
`test_query_intent_state_requirements_rejects_unsafe_intent_id` tests, which still
pass unmodified).

### Verification
New test `test_intent_sparql_matches_real_ontology_content` in `tests/test_broker.py`:
loads `ontology/topology.ttl` into a real `rdflib.Graph()`, calls
`broker._build_intent_sparql("intent-core-health-01")`, executes that query text via
`graph.query(sparql)` (no HTTP mocking at all), and asserts:
- exactly 2 rows returned,
- the `(device, interfaceName)` pairs are exactly `("Router-A", "GigabitEthernet0/0/0")`
  and `("Router-B", "GigabitEthernet0/0/1")`,
- every row has `metric == "input_drops"`, `operator == "=="`,
  `targetValue == "0.0"`.

This was manually spot-checked first by running the query against the real ontology
file outside of pytest to confirm the refactor produces the right shape before writing
assertions — it returned exactly the 2 expected rows.

```
$ venv/bin/pytest tests/test_broker.py -v
15 passed in 1.25s
```

---

## Final full-suite run

```
$ cd /workspace/contextbroker && venv/bin/pytest tests/ -v
tests/test_broker.py ............... (15 passed)
tests/test_docker_compose.py .       (1 passed)
tests/test_seed_graph.py ...         (3 passed)
tests/test_telemetry_simulator.py ...... (6 passed)

============================== 25 passed in 1.25s ===============================
```

25/25 passed (17 original + 8 new: 2 for Finding 2, 4 for Findings 3/4, 1 for
Finding 5, 1 for Finding 6).

## Files modified
- `/workspace/contextbroker/docker-compose.yml`
- `/workspace/contextbroker/telemetry_simulator.py`
- `/workspace/contextbroker/broker.py`
- `/workspace/contextbroker/tests/test_telemetry_simulator.py`
- `/workspace/contextbroker/tests/test_broker.py`

`ontology/topology.ttl` and `tests/conftest.py` were read but not modified (topology.ttl
was correct; the new Finding 6 test uses it as-is and it passed on the first real
non-mocked run).

## Concerns
- Docker itself could not be exercised in this sandbox (no `docker`/`docker compose`
  binary available), so Finding 1's fix is verified by source inspection of the real
  `stain/jena-fuseki` Dockerfile and entrypoint script (cited above) plus the existing
  service/port-name unit test, not by an actual container run. A human with Docker
  access should do one real `docker compose up fuseki` smoke test before relying on
  this in a live demo.
- `stain/jena-fuseki` is a third-party (not Apache-official) image maintained by a
  single GitHub user (`stain`); it's the only verified-to-exist option matching the
  required CLI invocation style found during research, but it is not an
  Apache-Software-Foundation-published artifact.

---

# Second fix pass (re-review of the first fix pass)

Addresses 1 new Important finding (R1, introduced by the Critical fix above) plus
4 Minor cleanups (R2, R3, R5, R6). No git operations performed.

Final full-suite run: **26/26 passed** (`venv/bin/pytest tests/ -v`).

---

## Finding R1 (Important): stain/jena-fuseki requires basic auth on the data-write endpoint

### Research: shiro.ini auth rules

Fetched
`https://raw.githubusercontent.com/stain/jena-docker/master/jena-fuseki/shiro.ini`
directly. The URL-pattern rules, in file order (Shiro `[urls]` sections are matched
top-to-bottom, first match wins):

```
/$/status = anon
/$/ping = anon
/$/** = authcBasic,user[admin]
/*/update/** = authcBasic,user[admin]
/*/data/** = authcBasic,user[admin]
/**=anon
```

Findings:
- `/*/update/**` (SPARQL Update protocol) and `/*/data/**` (Graph Store Protocol,
  which `seed_graph.py`'s `.../network-dataset/data` POST hits) both require
  `authcBasic` with the `admin` role. **Confirms the finding**: an unauthenticated
  `seed_graph.py` run against a real `stain/jena-fuseki` container would get
  HTTP 401.
- **`/*/query/**` has no explicit rule** in the file. It doesn't match `/$/**`
  (different first path segment), `/*/update/**`, or `/*/data/**`, so it falls
  through to the trailing catch-all `/**=anon` — i.e. **the SPARQL query endpoint
  is open, anonymous access is permitted**. `broker.py`'s
  `query_intent_state_requirements`, which POSTs to `.../network-dataset/query`,
  does **not** need auth added.
- The container's admin password is randomly generated on startup unless the
  `ADMIN_PASSWORD` env var is set (per the entrypoint script inspected in the first
  pass), so without pinning it, no fixed credential pair could work reliably anyway.

### Fix

`docker-compose.yml` — pin a fixed local-dev admin password on the `fuseki` service:
```diff
  fuseki:
    image: stain/jena-fuseki:5.1.0
    container_name: fuseki
    ports:
      - "3030:3030"
+   environment:
+     - ADMIN_PASSWORD=admin
    command: ["/jena-fuseki/fuseki-server", "--update", "--mem", "/network-dataset"]
    restart: unless-stopped
```

`seed_graph.py` — added an optional `auth` parameter to `load_ttl_to_fuseki`,
defaulting to the matching credential pair, and passed it through to the POST:
```python
def load_ttl_to_fuseki(
    ttl_path: pathlib.Path,
    data_endpoint: str,
    auth: tuple[str, str] | None = ("admin", "admin"),
) -> requests.Response:
    payload = pathlib.Path(ttl_path).read_bytes()
    response = requests.post(
        data_endpoint,
        data=payload,
        headers={"Content-Type": "text/turtle"},
        auth=auth,
    )
    response.raise_for_status()
    return response
```
The default keeps the un-parameterized call (as used by `__main__`) working
out of the box against a container started from the updated compose file.

`broker.py` was **not** modified for R1 — per the research above, its query
endpoint is genuinely open under the anon catch-all, so adding auth there would be
unnecessary.

### Verification
Updated `test_load_ttl_to_fuseki_posts_raw_turtle_bytes` in
`tests/test_seed_graph.py` to assert `kwargs["auth"] == ("admin", "admin")`.

```
$ venv/bin/pytest tests/test_seed_graph.py tests/test_broker.py -v
tests/test_seed_graph.py::test_topology_ttl_parses_and_defines_intent_core_health_01 PASSED
tests/test_seed_graph.py::test_load_ttl_to_fuseki_posts_raw_turtle_bytes PASSED
tests/test_seed_graph.py::test_load_ttl_to_fuseki_raises_on_http_error PASSED
tests/test_broker.py (15 passed, unchanged)
18 passed
```

---

## Finding R2 (Minor): test_docker_compose.py doesn't lock the Fuseki image or command

### Fix
Added to `tests/test_docker_compose.py`, in the existing test:
```python
assert services["fuseki"]["image"] == "stain/jena-fuseki:5.1.0"
fuseki_command = services["fuseki"]["command"]
assert isinstance(fuseki_command, list)
assert fuseki_command[0].endswith("fuseki-server")
```

### Verification
```
$ venv/bin/pytest tests/test_docker_compose.py -v
tests/test_docker_compose.py::test_docker_compose_defines_victoriametrics_and_fuseki PASSED
1 passed
```

---

## Finding R3 (Minor): redundant duplicate tool-name allow-list in broker.py

### Fix
Removed the hand-typed module-level literal
`_ALLOWED_TOOL_NAMES = {"query_intent_state_requirements", "query_tsdb_metrics"}`
from `broker.py`. The dispatch check in `run_agent` now validates directly against
`_TOOL_ALLOWED_ARGS` (already keyed by tool name, derived from `TOOLS`):
```diff
-                if name not in _ALLOWED_TOOL_NAMES:
+                if name not in _TOOL_ALLOWED_ARGS:
                     raise ValueError(f"Unknown tool: {name!r}")
```
Pure internal refactor — no behavior change; the set of valid tool names is
identical either way since `_TOOL_ALLOWED_ARGS` is keyed by the same two names.

### Verification
```
$ venv/bin/pytest tests/test_broker.py -v
15 passed
```
No test changes were needed for this finding, as expected.

---

## Finding R5 (Minor): no test for inject_drops propagation through build_payload/run

### Fix
Added `test_build_payload_propagates_inject_drops_flag` to
`tests/test_telemetry_simulator.py`. It calls `sim.build_payload(rng, inject_drops=True)`
across 200 fixed seeds (0-199) and parses `input_drops=` out of each generated line,
asserting at least one nonzero value is observed across the trials. A companion loop
over the same 200 seeds asserts `build_payload(rng, inject_drops=False)` — and the
no-argument default form — never produces a nonzero `input_drops` value. Using a
fixed range of seeds keeps the test deterministic (not flaky).

### Verification
```
$ venv/bin/pytest tests/test_telemetry_simulator.py -v
7 passed (6 original + 1 new)
```

---

## Finding R6 (Minor): loosen-to-tighten a test assertion

### Fix
`test_run_agent_stops_after_max_iterations_without_raising` in `tests/test_broker.py`
previously asserted the loose form:
```python
assert fake_client.chat.completions.create.call_count <= broker.MAX_ITERATIONS
```
Tightened to:
```python
assert fake_client.chat.completions.create.call_count == broker.MAX_ITERATIONS
```
since the mock client always returns a tool call, so `run_agent` must hit the cap
exactly (`for _ in range(MAX_ITERATIONS)` with no early return possible).

### Verification
```
$ venv/bin/pytest tests/test_broker.py -v
15 passed
```

---

## Second-pass final full-suite run

```
$ cd /workspace/contextbroker && venv/bin/pytest tests/ -v
tests/test_broker.py ...............          (15 passed)
tests/test_docker_compose.py .                 (1 passed)
tests/test_seed_graph.py ...                   (3 passed)
tests/test_telemetry_simulator.py .......      (7 passed)

============================== 26 passed in 1.23s ==============================
```

26/26 passed (25 from the first pass + 1 new test for R5; R1/R2/R3/R6 modified
existing code/tests without changing the total count).

## Files modified (second pass)
- `/workspace/contextbroker/docker-compose.yml` (R1)
- `/workspace/contextbroker/seed_graph.py` (R1)
- `/workspace/contextbroker/broker.py` (R3)
- `/workspace/contextbroker/tests/test_seed_graph.py` (R1)
- `/workspace/contextbroker/tests/test_docker_compose.py` (R2)
- `/workspace/contextbroker/tests/test_telemetry_simulator.py` (R5)
- `/workspace/contextbroker/tests/test_broker.py` (R6)

`broker.py` was read and considered for R1 but not modified for auth (query endpoint
confirmed open per shiro.ini research above); it was modified only for R3.

## Concerns
- Same as the first pass: Docker itself is unavailable in this sandbox, so the
  `ADMIN_PASSWORD=admin` / `auth=("admin","admin")` fix is verified by reading the
  real `shiro.ini` and by the mock-based unit test, not by an actual
  `docker compose up` + live POST against a real Fuseki container. A human with
  Docker access should do one real end-to-end smoke test (`seed_graph.py` against a
  freshly started container) before relying on this in a live demo.
- `admin`/`admin` is a throwaway local-dev credential for a sandboxed demo project,
  consistent with the existing placeholder `.env` pattern elsewhere in this project;
  it is not intended to be a real secret and should not be reused outside local
  development.

