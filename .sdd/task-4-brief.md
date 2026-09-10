### Task 4: Broker Tool Functions (SPARQL + PromQL reads)

**Files:**
- Create: `broker.py`
- Test: `tests/test_broker.py`

**Interfaces:**
- Consumes: ontology predicate names from Task 2's `ontology/topology.ttl`: `hasTargetMetric`, `hasOperator`, `hasTargetValue`, `appliesToLink`, `hasInterface`, `onDevice`, `interfaceName`, namespace `http://example.org/network-ontology#`, individual `intent-core-health-01`. Metric-naming convention from Task 3's `telemetry_simulator.py`: `interface_metrics_<field>` with `device`/`interface` labels.
- Produces: `query_intent_state_requirements(intent_id: str, query_endpoint: str = FUSEKI_QUERY_ENDPOINT) -> dict` returning `{"metric": str, "operator": str, "target_value": float, "interfaces": [{"device": str, "interface_name": str}, ...]}`; `query_tsdb_metrics(device: str, interface: str, metric_type: str, time_window: str, query_endpoint: str = VM_QUERY_ENDPOINT) -> float | None`. A later task (Task 5) wraps both as LLM tools by these exact names and keyword-argument signatures — do not rename them or change their parameter names.

**Global constraints for this task:**
- **Do not use git.** No `git add`/`git commit` — just create/edit the local files.
- Fuseki query endpoint: `http://localhost:3030/network-dataset/query`. VictoriaMetrics query endpoint: `http://localhost:8428/api/v1/query`.
- No new runtime dependency — only `requests` (already in `requirements.txt`) and stdlib `re`.
- Both tool functions must validate their string inputs before interpolating them into a SPARQL/PromQL query string, and raise `ValueError` on anything containing characters outside a safe allowlist (letters, digits, underscore, hyphen, slash for tokens; digits+unit-letter for the time window). This is a deliberate injection guard — these functions build query strings via f-string interpolation of externally-supplied arguments (ultimately from an LLM tool call in Task 5), so unvalidated input is a real injection risk into the Fuseki/VictoriaMetrics query language.
- venv already exists at `contextbroker/venv/` — reuse it (`source venv/bin/activate` or run `venv/bin/pytest` directly).

- [ ] **Step 1: Write failing tests for both tool functions and their input validation**

Create `tests/test_broker.py`:

```python
from unittest.mock import Mock, patch

import pytest

import broker


SPARQL_RESPONSE = {
    "results": {
        "bindings": [
            {
                "metric": {"value": "input_drops"},
                "operator": {"value": "=="},
                "targetValue": {"value": "0.0"},
                "device": {"value": "Router-A"},
                "interfaceName": {"value": "GigabitEthernet0/0/0"},
            },
            {
                "metric": {"value": "input_drops"},
                "operator": {"value": "=="},
                "targetValue": {"value": "0.0"},
                "device": {"value": "Router-B"},
                "interfaceName": {"value": "GigabitEthernet0/0/1"},
            },
        ]
    }
}

VM_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [{"metric": {}, "value": [1700000000, "3"]}],
    },
}

VM_EMPTY_RESPONSE = {"status": "success", "data": {"resultType": "vector", "result": []}}


def test_query_intent_state_requirements_parses_sparql_json():
    mock_response = Mock()
    mock_response.json.return_value = SPARQL_RESPONSE
    mock_response.raise_for_status = Mock()

    with patch("broker.requests.post", return_value=mock_response) as mock_post:
        result = broker.query_intent_state_requirements("intent-core-health-01")

    assert result["metric"] == "input_drops"
    assert result["operator"] == "=="
    assert result["target_value"] == 0.0
    assert result["interfaces"] == [
        {"device": "Router-A", "interface_name": "GigabitEthernet0/0/0"},
        {"device": "Router-B", "interface_name": "GigabitEthernet0/0/1"},
    ]
    _, kwargs = mock_post.call_args
    assert "intent-core-health-01" in kwargs["data"]["query"]


def test_query_intent_state_requirements_rejects_unsafe_intent_id():
    with pytest.raises(ValueError):
        broker.query_intent_state_requirements("intent-1 . } DROP ALL ; #")


def test_query_tsdb_metrics_parses_vector_result():
    mock_response = Mock()
    mock_response.json.return_value = VM_RESPONSE
    mock_response.raise_for_status = Mock()

    with patch("broker.requests.get", return_value=mock_response) as mock_get:
        result = broker.query_tsdb_metrics(
            device="Router-A",
            interface="GigabitEthernet0/0/0",
            metric_type="input_drops",
            time_window="5m",
        )

    assert result == 3.0
    _, kwargs = mock_get.call_args
    query = kwargs["params"]["query"]
    assert "interface_metrics_input_drops" in query
    assert 'device="Router-A"' in query
    assert 'interface="GigabitEthernet0/0/0"' in query
    assert "[5m]" in query


def test_query_tsdb_metrics_returns_none_when_no_data():
    mock_response = Mock()
    mock_response.json.return_value = VM_EMPTY_RESPONSE
    mock_response.raise_for_status = Mock()

    with patch("broker.requests.get", return_value=mock_response):
        result = broker.query_tsdb_metrics(
            device="Router-A",
            interface="GigabitEthernet0/0/0",
            metric_type="input_drops",
            time_window="5m",
        )

    assert result is None


@pytest.mark.parametrize(
    "device,interface,metric_type,time_window",
    [
        ("Router-A; DROP", "GigabitEthernet0/0/0", "input_drops", "5m"),
        ("Router-A", "Gi0/0/0\"}", "input_drops", "5m"),
        ("Router-A", "GigabitEthernet0/0/0", "input_drops", "5m; rm -rf"),
    ],
)
def test_query_tsdb_metrics_rejects_unsafe_inputs(device, interface, metric_type, time_window):
    with pytest.raises(ValueError):
        broker.query_tsdb_metrics(
            device=device, interface=interface, metric_type=metric_type, time_window=time_window
        )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_broker.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'broker'`.

- [ ] **Step 3: Write the tool-function half of broker.py**

Create `broker.py`:

```python
"""Context broker: compares live TSDB telemetry against graph-declared intent."""
import re

import requests

FUSEKI_QUERY_ENDPOINT = "http://localhost:3030/network-dataset/query"
VM_QUERY_ENDPOINT = "http://localhost:8428/api/v1/query"

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_\-/]+$")
_SAFE_WINDOW = re.compile(r"^[0-9]+[smhd]$")


def _validate_token(value: str, label: str) -> str:
    if not _SAFE_TOKEN.match(value):
        raise ValueError(f"Unsafe {label!r}: {value!r}")
    return value


def query_intent_state_requirements(
    intent_id: str, query_endpoint: str = FUSEKI_QUERY_ENDPOINT
) -> dict:
    _validate_token(intent_id, "intent_id")

    sparql = f"""
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
        f'max_over_time(interface_metrics_{metric_type}'
        f'{{device="{device}", interface="{interface}"}}[{time_window}])'
    )

    response = requests.get(query_endpoint, params={"query": promql})
    response.raise_for_status()
    result = response.json()["data"]["result"]

    if not result:
        return None
    return float(result[0]["value"][1])
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_broker.py -v
```

Expected: PASS (6 tests).

- [ ] **Step 5: Note the manual-Docker-verification step in your report**

This step needs a real Fuseki + VictoriaMetrics stack with seeded data (not available in this sandbox) — do not attempt it, just note in your report that it's deferred:

```bash
docker compose up -d
source venv/bin/activate
python -c "import broker; print(broker.query_intent_state_requirements('intent-core-health-01'))"
python -c "import broker; print(broker.query_tsdb_metrics(device='Router-A', interface='GigabitEthernet0/0/0', metric_type='input_drops', time_window='5m'))"
```

Do NOT run a "Commit" step — this project is not using git. Skip any git commands entirely.
