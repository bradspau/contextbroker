# Planning: Reproducing the Autonomous Context Broker

**For:** a fresh Claude Code session with no memory of building this the first time.

**Goal:** build a CLI agent that answers "is network intent X compliant right
now?" by comparing live telemetry in VictoriaMetrics against expectations
declared as OWL/RDFS in Apache Jena Fuseki, using an OpenAI-compatible LLM to
do the comparison.

This plan encodes the **corrected, working design** — the first build of
this project went through two rounds of bug fixes after an initial pass and
two rounds of code review. Building from this plan should get you a working
system on the first attempt; the "Lessons Learned" section at the end
explains what went wrong the first time and why each fix below is there.

## Architecture

Two Docker-Compose services (VictoriaMetrics for telemetry, Fuseki for the
intent/topology graph), a synthetic telemetry generator, and a Python
`broker.py` exposing two LLM tool functions — one SPARQL-backed, one
PromQL-backed — wired into a standard OpenAI tool-calling loop.

## Global Constants (use verbatim everywhere below)

- Project root: wherever you're building this (all paths below are relative to it)
- Runtime deps: exactly `requests`, `openai`, `python-dotenv` — nothing else in `requirements.txt`
- Dev/test-only deps (never in `requirements.txt`): `pytest`, `pyyaml`, `rdflib`
- Ports: VictoriaMetrics `8428`, Fuseki `3030`
- Fuseki dataset name: `network-dataset`, used verbatim in every URL
- Fuseki admin credentials: username `admin`, password `admin` (local-dev only, matches `ADMIN_PASSWORD` env var on the container — see Gotcha 2)
- Env vars (in `.env`): `LLM_API_KEY`, `LLM_BASE_URL`, `MODEL_NAME`
- Ontology namespace: `http://example.org/network-ontology#`
- Fixture: intent `intent-core-health-01` (zero input drops) applies to the link between `Router-A` (`GigabitEthernet0/0/0`) and `Router-B` (`GigabitEthernet0/0/1`)
- Telemetry: measurement `interface_metrics`, fields `in_octets`/`out_octets`/`input_drops`, write endpoint `http://localhost:8428/write`, 5s interval

## File Structure

```
docker-compose.yml
requirements.txt
.env                    # placeholders, gitignored
.gitignore
ontology/topology.ttl
seed_graph.py
telemetry_simulator.py
broker.py
tests/
  conftest.py
  test_docker_compose.py
  test_seed_graph.py
  test_telemetry_simulator.py
  test_broker.py
```

---

## Step 1: Scaffolding

Create a venv, install `requests openai python-dotenv`, freeze to
`requirements.txt` (don't hand-pin versions — resolve fresh at build time).
Install `pytest pyyaml rdflib` separately for tests, but do not freeze them.

`.gitignore`:
```
.env
venv/
__pycache__/
.pytest_cache/
*.pyc
```

`.env` (placeholders — fill in real values before running the broker):
```
LLM_API_KEY=your-api-key-here
LLM_BASE_URL=https://api.openai.com/v1
MODEL_NAME=gpt-4o-mini
```

`docker-compose.yml`:
```yaml
services:
  victoriametrics:
    image: victoriametrics/victoria-metrics:v1.102.0
    container_name: victoriametrics
    ports:
      - "8428:8428"
    volumes:
      - vm-data:/victoria-metrics-data
    restart: unless-stopped

  fuseki:
    image: stain/jena-fuseki:5.1.0
    container_name: fuseki
    ports:
      - "3030:3030"
    environment:
      - ADMIN_PASSWORD=admin
    command: ["/jena-fuseki/fuseki-server", "--update", "--mem", "/network-dataset"]
    restart: unless-stopped

volumes:
  vm-data:
```

**Test:** `tests/test_docker_compose.py` — load the YAML, assert
`services["victoriametrics"]["ports"]` contains `"8428:8428"`,
`services["fuseki"]["ports"]` contains `"3030:3030"`,
`services["fuseki"]["image"] == "stain/jena-fuseki:5.1.0"`, and
`services["fuseki"]["command"][0].endswith("fuseki-server")` (this last
assertion is what would have caught Gotcha 1 immediately — see below).

**`tests/conftest.py`** (needed so `pytest` can find the top-level modules
regardless of invocation directory):
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
```

---

## Step 2: Ontology and graph seeding

`ontology/topology.ttl`:
```turtle
@prefix : <http://example.org/network-ontology#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

### Classes ###

:OperationalIntent a owl:Class ;
    rdfs:label "Operational Intent" .

:TopologicalLink a owl:Class ;
    rdfs:label "Topological Link" .

:Interface a owl:Class ;
    rdfs:label "Interface" .

### Properties ###

:hasTargetMetric a owl:DatatypeProperty ;
    rdfs:domain :OperationalIntent ;
    rdfs:range xsd:string .

:hasOperator a owl:DatatypeProperty ;
    rdfs:domain :OperationalIntent ;
    rdfs:range xsd:string .

:hasTargetValue a owl:DatatypeProperty ;
    rdfs:domain :OperationalIntent ;
    rdfs:range xsd:double .

:appliesToLink a owl:ObjectProperty ;
    rdfs:domain :OperationalIntent ;
    rdfs:range :TopologicalLink .

:hasInterface a owl:ObjectProperty ;
    rdfs:domain :TopologicalLink ;
    rdfs:range :Interface .

:onDevice a owl:DatatypeProperty ;
    rdfs:domain :Interface ;
    rdfs:range xsd:string .

:interfaceName a owl:DatatypeProperty ;
    rdfs:domain :Interface ;
    rdfs:range xsd:string .

### Individuals ###

:if-router-a-ge000 a :Interface ;
    :onDevice "Router-A" ;
    :interfaceName "GigabitEthernet0/0/0" .

:if-router-b-ge001 a :Interface ;
    :onDevice "Router-B" ;
    :interfaceName "GigabitEthernet0/0/1" .

:link-router-a-router-b a :TopologicalLink ;
    rdfs:label "Router-A <-> Router-B core link" ;
    :hasInterface :if-router-a-ge000 , :if-router-b-ge001 .

:intent-core-health-01 a :OperationalIntent ;
    rdfs:label "Core Health: Zero Input Drops" ;
    :hasTargetMetric "input_drops" ;
    :hasOperator "==" ;
    :hasTargetValue 0.0 ;
    :appliesToLink :link-router-a-router-b .
```

`seed_graph.py`:
```python
"""Loads ontology/topology.ttl into a Fuseki dataset via the Graph Store Protocol."""
import pathlib
import sys

import requests

DEFAULT_TTL_PATH = pathlib.Path(__file__).resolve().parent / "ontology" / "topology.ttl"
DEFAULT_DATA_ENDPOINT = "http://localhost:3030/network-dataset/data"


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


if __name__ == "__main__":
    ttl_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TTL_PATH
    endpoint = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DATA_ENDPOINT

    resp = load_ttl_to_fuseki(ttl_path, endpoint)
    print(f"Loaded {ttl_path} into {endpoint} — HTTP {resp.status_code}")
```

**Tests (`tests/test_seed_graph.py`):**
1. `topology.ttl` parses with `rdflib` and contains the `intent-core-health-01` triples (`hasTargetMetric == "input_drops"`, `hasOperator == "=="`).
2. `load_ttl_to_fuseki` (mock `requests.post`) POSTs the raw file bytes with `headers={"Content-Type": "text/turtle"}` and `auth=("admin", "admin")` by default.
3. It calls `raise_for_status()` — an HTTP error propagates as an exception, not swallowed.

---

## Step 3: Telemetry simulator

`telemetry_simulator.py`:
```python
"""Generates synthetic interface telemetry and writes it to VictoriaMetrics."""
import argparse
import random
import time

import requests

WRITE_ENDPOINT = "http://localhost:8428/write"
MEASUREMENT_NAME = "interface_metrics"
ROUTERS = [
    ("Router-A", "GigabitEthernet0/0/0"),
    ("Router-B", "GigabitEthernet0/0/1"),
]
POST_INTERVAL_SECONDS = 5


def format_line_protocol(device: str, interface: str, metrics: dict, timestamp_ns: int) -> str:
    tags = f"device={device},interface={interface}"
    fields = ",".join(f"{key}={value}" for key, value in metrics.items())
    return f"{MEASUREMENT_NAME},{tags} {fields} {timestamp_ns}"


def generate_metrics(rng: random.Random, inject_drops: bool = False) -> dict:
    if inject_drops:
        input_drops = 0 if rng.random() > 0.05 else rng.randint(1, 20)
    else:
        input_drops = 0
    return {
        "in_octets": rng.randint(1_000_000, 5_000_000),
        "out_octets": rng.randint(1_000_000, 5_000_000),
        "input_drops": input_drops,
    }


def build_payload(rng: random.Random, inject_drops: bool = False) -> str:
    timestamp_ns = time.time_ns()
    lines = [
        format_line_protocol(device, interface, generate_metrics(rng, inject_drops), timestamp_ns)
        for device, interface in ROUTERS
    ]
    return "\n".join(lines)


def post_metrics(write_endpoint: str, payload: str) -> None:
    response = requests.post(write_endpoint, data=payload)
    response.raise_for_status()


def run(write_endpoint: str = WRITE_ENDPOINT, once: bool = False, inject_drops: bool = False) -> None:
    rng = random.Random()
    while True:
        post_metrics(write_endpoint, build_payload(rng, inject_drops))
        if once:
            return
        time.sleep(POST_INTERVAL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Send a single batch and exit")
    parser.add_argument(
        "--inject-drops",
        action="store_true",
        help="Randomly inject nonzero input_drops (5%% chance per sample) to demo the non-compliant path",
    )
    args = parser.parse_args()
    run(once=args.once, inject_drops=args.inject_drops)
```

Note the `inject_drops` default is `False` — see Gotcha 3 for why this
matters.

**Tests (`tests/test_telemetry_simulator.py`):**
1. `format_line_protocol` builds the exact expected line-protocol string.
2. `generate_metrics(rng, inject_drops=False)` (the default) always returns `input_drops == 0`, deterministically, across many seeds.
3. `generate_metrics(rng, inject_drops=True)` can return a nonzero `input_drops` across many seeds (don't assert an exact rate).
4. `build_payload` includes both routers, one line each.
5. `build_payload(rng, inject_drops=True)` actually propagates the flag down to `generate_metrics` (test this separately from #2/#3, which only call `generate_metrics` directly — a dropped parameter in `build_payload`'s pass-through wouldn't otherwise be caught).
6. `post_metrics` POSTs the payload to the given endpoint and calls `raise_for_status()`.

---

## Step 4 & 5: broker.py (tool functions + orchestration loop + CLI)

```python
"""Context broker: compares live TSDB telemetry against graph-declared intent."""
import json
import os
import re
import sys

import requests
from dotenv import load_dotenv
from openai import OpenAI

from telemetry_simulator import MEASUREMENT_NAME

FUSEKI_QUERY_ENDPOINT = "http://localhost:3030/network-dataset/query"
VM_QUERY_ENDPOINT = "http://localhost:8428/api/v1/query"
MAX_ITERATIONS = 10

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_\-/]+$")
_SAFE_WINDOW = re.compile(r"^[0-9]+[smhd]$")


def _validate_token(value: str, label: str) -> str:
    if not _SAFE_TOKEN.match(value):
        raise ValueError(f"Unsafe {label!r}: {value!r}")
    return value


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


def query_intent_state_requirements(
    intent_id: str, query_endpoint: str = FUSEKI_QUERY_ENDPOINT
) -> dict:
    sparql = _build_intent_sparql(intent_id)

    response = requests.post(
        query_endpoint,
        data={"query": sparql},
        headers={"Accept": "application/sparql-results+json"},
    )
    response.raise_for_status()
    bindings = response.json()["results"]["bindings"]

    if not bindings:
        raise ValueError(f"No such intent: {intent_id!r}")

    first = bindings[0]
    return {
        "metric": first["metric"]["value"],
        "operator": first["operator"]["value"],
        "target_value": float(first["targetValue"]["value"]),
        "interfaces": [
            {
                "device": row["device"]["value"],
                "interface_name": row["interfaceName"]["value"],
            }
            for row in bindings
        ],
    }


def query_tsdb_metrics(
    device: str,
    interface: str,
    metric_type: str,
    time_window: str,
    query_endpoint: str = VM_QUERY_ENDPOINT,
) -> float | None:
    _validate_token(device, "device")
    _validate_token(interface, "interface")
    _validate_token(metric_type, "metric_type")
    if not _SAFE_WINDOW.match(time_window):
        raise ValueError(f"Unsafe time_window: {time_window!r}")

    promql = (
        f'max_over_time({MEASUREMENT_NAME}_{metric_type}'
        f'{{device="{device}", interface="{interface}"}}[{time_window}])'
    )

    response = requests.get(query_endpoint, params={"query": promql})
    response.raise_for_status()
    result = response.json()["data"]["result"]

    if not result:
        return None
    return float(result[0]["value"][1])


SYSTEM_PROMPT = """You are a network compliance agent. You have two tools:

- query_intent_state_requirements(intent_id): reads the declared requirement for an
  operational intent from the knowledge graph (target metric, operator, target value,
  and the interfaces it applies to).
- query_tsdb_metrics(device, interface, metric_type, time_window): reads the observed
  value of a metric over a recent time window from the telemetry database.

To answer a compliance question: first call query_intent_state_requirements to learn
what is expected and which interfaces to check. Then call query_tsdb_metrics for each
of those interfaces to learn what is actually happening. Mathematically compare the
observed value against the target value using the given operator, and state plainly
whether the intent is currently compliant, citing the numbers you compared."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_intent_state_requirements",
            "description": "Read the declared requirement for an operational intent from the knowledge graph.",
            "parameters": {
                "type": "object",
                "properties": {"intent_id": {"type": "string"}},
                "required": ["intent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_tsdb_metrics",
            "description": "Read the observed value of a metric over a time window from the TSDB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "interface": {"type": "string"},
                    "metric_type": {"type": "string"},
                    "time_window": {"type": "string"},
                },
                "required": ["device", "interface", "metric_type", "time_window"],
            },
        },
    },
]

# Derive each tool's allowed argument keys from its own declared JSON-schema
# properties in TOOLS, so the allow-list can never drift from the schema.
_TOOL_ALLOWED_ARGS = {
    tool["function"]["name"]: set(tool["function"]["parameters"]["properties"])
    for tool in TOOLS
}


def _build_client() -> OpenAI:
    load_dotenv()
    return OpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"])


def run_agent(user_prompt: str, client: OpenAI | None = None, model: str | None = None) -> str:
    if client is None:
        client = _build_client()
    if model is None:
        load_dotenv()
        model = os.environ["MODEL_NAME"]

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    for _ in range(MAX_ITERATIONS):
        response = client.chat.completions.create(model=model, messages=messages, tools=TOOLS)
        message = response.choices[0].message

        if not message.tool_calls:
            return message.content

        messages.append(
            {"role": "assistant", "content": message.content, "tool_calls": message.tool_calls}
        )
        for tool_call in message.tool_calls:
            try:
                arguments = json.loads(tool_call.function.arguments)
                name = tool_call.function.name
                # Restrict tool dispatch to the allow-list (derived from TOOLS,
                # so it can never drift from the declared schema).
                if name not in _TOOL_ALLOWED_ARGS:
                    raise ValueError(f"Unknown tool: {name!r}")
                # Reject any argument key the tool's own schema doesn't declare
                # (prevents e.g. an LLM-supplied query_endpoint override / SSRF).
                allowed_keys = _TOOL_ALLOWED_ARGS[name]
                extra_keys = set(arguments) - allowed_keys
                if extra_keys:
                    raise ValueError(
                        f"Unexpected argument(s) for tool {name!r}: {sorted(extra_keys)}"
                    )
                func = globals()[name]
                result = func(**arguments)
                content = json.dumps(result)
            except Exception as exc:
                content = json.dumps({"error": str(exc)})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": content,
                }
            )

    return f"Unable to reach a final answer after {MAX_ITERATIONS} tool-calling iterations."


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) or input("Ask the broker: ")
    print(run_agent(prompt))
```

**Tests (`tests/test_broker.py`):**
1. `query_intent_state_requirements` parses a mocked SPARQL JSON response into `{"metric", "operator", "target_value", "interfaces"}`.
2. `query_intent_state_requirements` rejects an unsafe `intent_id` (e.g. containing `.`, `}`, `;`) with `ValueError`, before any HTTP call.
3. `query_tsdb_metrics` parses a mocked VictoriaMetrics vector result into a `float`.
4. `query_tsdb_metrics` returns `None` when the vector result is empty.
5. `query_tsdb_metrics` rejects unsafe `device`/`interface`/`time_window` values (parametrized: `"Router-A; DROP"`, `'Gi0/0/0"}'`, `"5m; rm -rf"`).
6. **Real, unmocked:** load `ontology/topology.ttl` with `rdflib`, call `_build_intent_sparql("intent-core-health-01")`, run that exact query text against the loaded graph, and assert it returns the 2 expected rows. Prove this isn't tautological by checking it would fail on a predicate typo (e.g. temporarily mutate `:onDevice` in a copy of the query and confirm 0 rows).
7. `run_agent` with a fake client: first response has a tool call, mock the tool function, second response is the final answer — assert the tool was called with the right keyword arguments, and the final content is returned.
8. `run_agent` with a fake client whose first response has no tool calls — returns immediately, one `create()` call.
9. `run_agent` rejects an LLM-supplied extra kwarg (e.g. `query_endpoint`) on a tool call — assert the real tool function is NOT called with it, and the error is fed back as a `role: "tool"` message rather than raised.
10. `run_agent` with a mock client that always returns a tool call — assert it stops after exactly `MAX_ITERATIONS` calls and returns the fallback string, rather than looping forever or raising.

---

## Manual end-to-end verification (needs Docker + a real LLM key — not part of the automated test suite)

```bash
docker compose up -d
source venv/bin/activate
python seed_graph.py
python telemetry_simulator.py --once
curl -s 'http://localhost:8428/api/v1/query?query=interface_metrics_in_octets'
nohup python telemetry_simulator.py > telemetry.log 2>&1 &
python broker.py "Is intent-core-health-01 compliant right now?"
```

Expect a compliant answer. Kill the simulator and restart it with
`--inject-drops` to see the non-compliant path.

---

## Lessons Learned (why the plan above looks the way it does)

These were all found and fixed during code review of the first build. Skip
them and you'll likely rediscover them the hard way.

1. **`apache/jena-fuseki` does not exist on Docker Hub.** It returns a 404.
   Apache only publishes a *build kit* for Fuseki, not a prebuilt image. Use
   `stain/jena-fuseki` instead, which is real, and note its entrypoint does
   a bare `exec "$@"` — so `command:` must include the full binary path
   (`/jena-fuseki/fuseki-server`), not just the flags.

2. **That image requires HTTP basic auth on write endpoints.** Its
   `shiro.ini` requires `authcBasic` on `/*/update/**` and `/*/data/**`
   (with a randomly generated password unless `ADMIN_PASSWORD` is set), but
   leaves `/*/query/**` open under the trailing anon catch-all. So: set
   `ADMIN_PASSWORD=admin` on the container, and pass matching `auth=` on
   `seed_graph.py`'s write POST — but `broker.py`'s read-only query POST
   needs no auth changes.

3. **A naive "5% chance of drops per sample" default makes the demo lie.**
   `query_tsdb_metrics` uses `max_over_time` over a multi-minute window; a
   single drop pins the answer to "non-compliant" for the rest of that
   window. At 5s sampling and a 5-minute window (60 samples), a 5%-per-sample
   drop chance makes the demo report non-compliant ~95% of the time. Default
   to zero drops, and put the interesting behavior behind an explicit
   `--inject-drops` flag.

4. **Restricting tool *dispatch* by name isn't enough — also restrict
   *arguments*.** Both tool functions accept an optional `query_endpoint`
   override for testability. If the LLM's tool-call JSON is trusted
   verbatim, it can redirect that endpoint anywhere (SSRF). Derive an
   allow-list of argument keys per tool from the `TOOLS` schema itself (so
   it can't drift), and reject any call carrying an undeclared key.

5. **Tool-call exceptions must not crash the orchestration loop.** An LLM
   can pick an invalid `time_window` (e.g. `"5min"` instead of `"5m"`) or
   name an unknown tool. Catch exceptions around dispatch and feed the error
   back as a `role: "tool"` message so the model gets a chance to correct
   itself — and once errors can trigger retries, cap the loop
   (`MAX_ITERATIONS`) so a persistently-failing exchange can't spin forever.

6. **Don't duplicate the measurement-name literal.** `"interface_metrics"`
   appeared in both the telemetry writer and the broker's PromQL builder;
   renaming one silently breaks the other with all tests still green (since
   every other test mocks the HTTP layer). Define it once
   (`MEASUREMENT_NAME` in `telemetry_simulator.py`) and import it into
   `broker.py`.

7. **At least one test must exercise the SPARQL query against the real
   ontology file, unmocked.** Every other broker test mocks the HTTP
   response, so a typo in a predicate name would pass all of them and only
   fail against a live Fuseki instance. Extracting `_build_intent_sparql`
   as its own function makes this testable with `rdflib` alone.
