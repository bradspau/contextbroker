### Task 2: Ontology and Graph Seeding

**Files:**
- Create: `ontology/topology.ttl`
- Create: `seed_graph.py`
- Test: `tests/test_seed_graph.py`

**Interfaces:**
- Consumes: Fuseki dataset name `network-dataset` and namespace `http://example.org/network-ontology#` (project-wide constants).
- Produces: `load_ttl_to_fuseki(ttl_path: pathlib.Path, data_endpoint: str) -> requests.Response` — a later task's SPARQL query depends on the exact predicate names defined here: `hasTargetMetric`, `hasOperator`, `hasTargetValue`, `appliesToLink`, `hasInterface`, `onDevice`, `interfaceName`, and the individual `intent-core-health-01`. Use these exact names verbatim — do not rename or restructure them.

**Global constraints for this task:**
- **Do not use git.** No `git add`/`git commit` — just create/edit the local files.
- Dev/test-only deps already installed in `contextbroker/venv/`: `pytest`, `pyyaml`, `rdflib` (from Task 1). Do not add `rdflib` to `requirements.txt` — it is test-only.
- Runtime deps already frozen in `requirements.txt` from Task 1 (`requests`, `openai`, `python-dotenv`) — `seed_graph.py` itself only needs `requests`.
- venv already exists at `contextbroker/venv/` — reuse it (`source venv/bin/activate`).

- [ ] **Step 1: Write the ontology file**

Create `ontology/topology.ttl`:

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

- [ ] **Step 2: Write a failing test that the ontology parses and contains the fixture**

Create `tests/test_seed_graph.py`:

```python
from pathlib import Path
from unittest.mock import Mock, patch

import rdflib

import seed_graph

ONTOLOGY_PATH = Path(__file__).resolve().parent.parent / "ontology" / "topology.ttl"
NS = rdflib.Namespace("http://example.org/network-ontology#")


def test_topology_ttl_parses_and_defines_intent_core_health_01():
    graph = rdflib.Graph()
    graph.parse(ONTOLOGY_PATH, format="turtle")

    assert len(graph) > 0
    intent = NS["intent-core-health-01"]
    assert (intent, NS.hasTargetMetric, rdflib.Literal("input_drops")) in graph
    assert (intent, NS.hasOperator, rdflib.Literal("==")) in graph


def test_load_ttl_to_fuseki_posts_raw_turtle_bytes():
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.raise_for_status = Mock()

    with patch("seed_graph.requests.post", return_value=mock_response) as mock_post:
        response = seed_graph.load_ttl_to_fuseki(
            ONTOLOGY_PATH, "http://localhost:3030/network-dataset/data"
        )

    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs["headers"] == {"Content-Type": "text/turtle"}
    assert kwargs["data"] == ONTOLOGY_PATH.read_bytes()
    assert response is mock_response


def test_load_ttl_to_fuseki_raises_on_http_error():
    mock_response = Mock()
    mock_response.raise_for_status.side_effect = Exception("500 Server Error")

    with patch("seed_graph.requests.post", return_value=mock_response):
        try:
            seed_graph.load_ttl_to_fuseki(
                ONTOLOGY_PATH, "http://localhost:3030/network-dataset/data"
            )
            assert False, "expected an exception"
        except Exception as exc:
            assert "500" in str(exc)
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_seed_graph.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'seed_graph'`.

- [ ] **Step 4: Write seed_graph.py**

Create `seed_graph.py`:

```python
"""Loads ontology/topology.ttl into a Fuseki dataset via the Graph Store Protocol."""
import pathlib
import sys

import requests

DEFAULT_TTL_PATH = pathlib.Path(__file__).resolve().parent / "ontology" / "topology.ttl"
DEFAULT_DATA_ENDPOINT = "http://localhost:3030/network-dataset/data"


def load_ttl_to_fuseki(ttl_path: pathlib.Path, data_endpoint: str) -> requests.Response:
    payload = pathlib.Path(ttl_path).read_bytes()
    response = requests.post(
        data_endpoint,
        data=payload,
        headers={"Content-Type": "text/turtle"},
    )
    response.raise_for_status()
    return response


if __name__ == "__main__":
    ttl_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TTL_PATH
    endpoint = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DATA_ENDPOINT

    resp = load_ttl_to_fuseki(ttl_path, endpoint)
    print(f"Loaded {ttl_path} into {endpoint} — HTTP {resp.status_code}")
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_seed_graph.py -v
```

Expected: PASS (3 tests).

- [ ] **Step 6: Note the manual-Docker-verification step in your report**

This step needs a real Fuseki container (not available in this sandbox) — do not attempt it, just note in your report that it's deferred:

```bash
docker compose up -d
source venv/bin/activate
python seed_graph.py
curl -s -X POST http://localhost:3030/network-dataset/query \
  --data-urlencode 'query=SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }' \
  -H 'Accept: application/sparql-results+json'
```

Do NOT run a "Commit" step — this project is not using git. Skip any git commands entirely.
