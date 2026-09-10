# Task 3: Telemetry Simulation Pipeline — Report

## Summary

Task 3 has been completed successfully. Implemented a synthetic telemetry generator that produces InfluxDB line-protocol data for two simulated routers with interface metrics.

## What Was Done

Followed Test-Driven Development (TDD) strictly:

1. **Step 1**: Created failing test file `tests/test_telemetry_simulator.py` with 4 test cases
   - `test_format_line_protocol_builds_expected_string()` — validates exact line-protocol format
   - `test_generate_metrics_returns_nonnegative_integers()` — validates metric generation
   - `test_build_payload_includes_both_routers()` — validates multi-router payload
   - `test_post_metrics_sends_payload_to_write_endpoint()` — validates HTTP POST behavior

2. **Step 2**: Verified tests failed with `ModuleNotFoundError: No module named 'telemetry_simulator'`

3. **Step 3**: Created implementation file `telemetry_simulator.py` with:
   - `format_line_protocol()` — constructs InfluxDB line protocol with measurement name `interface_metrics`, tags `device` and `interface`, and fields `in_octets`, `out_octets`, `input_drops`
   - `generate_metrics()` — produces random metric values with weighted drops (95% zero, 5% 1-20)
   - `build_payload()` — generates timestamped payload for both routers
   - `post_metrics()` — sends payload via HTTP POST to VictoriaMetrics write endpoint
   - `run()` — main loop with 5-second interval and `--once` CLI flag for manual testing

4. **Step 4**: Verified all 4 tests pass

## Commands Executed

### Test Run (Final):
```bash
PYTHONPATH=/workspace/contextbroker /workspace/contextbroker/venv/bin/pytest tests/test_telemetry_simulator.py -v
```

### Output:
```
============================= test session starts ==============================
platform linux -- Python 3.11.2, pytest-9.1.1, pluggy-1.6.0 -- /workspace/contextbroker/venv/bin/python
cachedir: .pytest_cache
rootdir: /workspace
configfile: pyproject.toml
plugins: anyio-4.15.1
collecting ... collected 4 items

tests/test_telemetry_simulator.py::test_format_line_protocol_builds_expected_string PASSED [ 25%]
tests/test_telemetry_simulator.py::test_generate_metrics_returns_nonnegative_integers PASSED [ 50%]
tests/test_telemetry_simulator.py::test_build_payload_includes_both_routers PASSED [ 75%]
tests/test_telemetry_simulator.py::test_post_metrics_sends_payload_to_write_endpoint PASSED [100%]

============================== 4 passed in 0.16s ===============================
```

## Files Created

1. **`/workspace/contextbroker/telemetry_simulator.py`** — Main implementation

```python
"""Generates synthetic interface telemetry and writes it to VictoriaMetrics."""
import argparse
import random
import time

import requests

WRITE_ENDPOINT = "http://localhost:8428/write"
ROUTERS = [
    ("Router-A", "GigabitEthernet0/0/0"),
    ("Router-B", "GigabitEthernet0/0/1"),
]
POST_INTERVAL_SECONDS = 5


def format_line_protocol(device: str, interface: str, metrics: dict, timestamp_ns: int) -> str:
    tags = f"device={device},interface={interface}"
    fields = ",".join(f"{key}={value}" for key, value in metrics.items())
    return f"interface_metrics,{tags} {fields} {timestamp_ns}"


def generate_metrics(rng: random.Random) -> dict:
    return {
        "in_octets": rng.randint(1_000_000, 5_000_000),
        "out_octets": rng.randint(1_000_000, 5_000_000),
        "input_drops": rng.choices([0, rng.randint(1, 20)], weights=[95, 5])[0],
    }


def build_payload(rng: random.Random) -> str:
    timestamp_ns = time.time_ns()
    lines = [
        format_line_protocol(device, interface, generate_metrics(rng), timestamp_ns)
        for device, interface in ROUTERS
    ]
    return "\n".join(lines)


def post_metrics(write_endpoint: str, payload: str) -> None:
    response = requests.post(write_endpoint, data=payload)
    response.raise_for_status()


def run(write_endpoint: str = WRITE_ENDPOINT, once: bool = False) -> None:
    rng = random.Random()
    while True:
        post_metrics(write_endpoint, build_payload(rng))
        if once:
            return
        time.sleep(POST_INTERVAL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="Send a single batch and exit (for manual verification)"
    )
    args = parser.parse_args()
    run(once=args.once)
```

2. **`/workspace/contextbroker/tests/test_telemetry_simulator.py`** — Comprehensive test suite

```python
import random
from unittest.mock import Mock, patch

import telemetry_simulator as sim


def test_format_line_protocol_builds_expected_string():
    line = sim.format_line_protocol(
        device="Router-A",
        interface="GigabitEthernet0/0/0",
        metrics={"in_octets": 12345, "out_octets": 6789, "input_drops": 0},
        timestamp_ns=1700000000000000000,
    )
    assert line == (
        "interface_metrics,device=Router-A,interface=GigabitEthernet0/0/0 "
        "in_octets=12345,out_octets=6789,input_drops=0 1700000000000000000"
    )


def test_generate_metrics_returns_nonnegative_integers():
    rng = random.Random(42)
    metrics = sim.generate_metrics(rng)

    assert set(metrics.keys()) == {"in_octets", "out_octets", "input_drops"}
    for value in metrics.values():
        assert isinstance(value, int)
        assert value >= 0


def test_build_payload_includes_both_routers():
    rng = random.Random(1)
    payload = sim.build_payload(rng)

    assert "device=Router-A,interface=GigabitEthernet0/0/0" in payload
    assert "device=Router-B,interface=GigabitEthernet0/0/1" in payload
    assert payload.count("\n") == 1  # two lines, one newline separator


def test_post_metrics_sends_payload_to_write_endpoint():
    mock_response = Mock()
    mock_response.raise_for_status = Mock()

    with patch("telemetry_simulator.requests.post", return_value=mock_response) as mock_post:
        sim.post_metrics("http://localhost:8428/write", "some,line=protocol value=1 1")

    mock_post.assert_called_once_with(
        "http://localhost:8428/write", data="some,line=protocol value=1 1"
    )
```

## Manual Verification (Docker Step)

The following step requires a real VictoriaMetrics container and is deferred (Docker is not available in this sandbox):

```bash
# Start VictoriaMetrics container
docker compose up -d

# Activate venv and send test batch
source venv/bin/activate
python telemetry_simulator.py --once

# Query metrics to verify ingestion
curl -s 'http://localhost:8428/api/v1/query?query=interface_metrics_in_octets'
```

To run continuously in background:
```bash
nohup python telemetry_simulator.py > telemetry.log 2>&1 &
echo $! > telemetry.pid
```

## Implementation Notes

- **Metric naming**: InfluxDB line-protocol measurement name is exactly `interface_metrics` with tags `device` and `interface` — VictoriaMetrics derives the metric names `interface_metrics_in_octets`, `interface_metrics_out_octets`, and `interface_metrics_input_drops` from this
- **Devices/Interfaces**: Hardcoded as `Router-A` / `GigabitEthernet0/0/0` and `Router-B` / `GigabitEthernet0/0/1`
- **Field values**: Random integers in range [1M, 5M] for octets; drops are 0 (95% weight) or 1-20 (5% weight)
- **Interval**: 5 seconds between POST batches
- **Testing**: Used mocking for HTTP requests to avoid external dependencies; all tests use deterministic random seeds for reproducibility
- **No git operations**: Followed instructions — created/edited plain files without git commits

## Concerns

None. The implementation:
- Follows the brief exactly
- Passes all tests
- Uses the correct line-protocol measurement and tag names required by Task 4's PromQL queries
- No external dependencies beyond `requests` (already in venv)
- No git operations performed

---

## Task 3 RNG Optimization Fix

### Change Made

**File:** `/workspace/contextbroker/telemetry_simulator.py` (line 26)

**Before:**
```python
"input_drops": rng.choices([0, rng.randint(1, 20)], weights=[95, 5])[0],
```

**After:**
```python
"input_drops": 0 if rng.random() > 0.05 else rng.randint(1, 20),
```

**Rationale:** The original implementation unconditionally called `rng.randint(1, 20)` even on ~95% of calls where the result was discarded, wasting an RNG draw. The fix uses a conditional to only draw the second random number when needed (when `rng.random() <= 0.05`), preserving the identical distribution while improving RNG efficiency.

### Test Command & Results

```bash
PYTHONPATH=/workspace/contextbroker /workspace/contextbroker/venv/bin/pytest tests/test_telemetry_simulator.py -v
```

**Output:**
```
============================= test session starts ==============================
platform linux -- Python 3.11.2, pytest-9.1.1, pluggy-1.6.0 -- /workspace/contextbroker/venv/bin/python
cachedir: .pytest_cache
rootdir: /workspace
configfile: pyproject.toml
plugins: anyio-4.15.1
collecting ... collected 4 items

tests/test_telemetry_simulator.py::test_format_line_protocol_builds_expected_string PASSED [ 25%]
tests/test_telemetry_simulator.py::test_generate_metrics_returns_nonnegative_integers PASSED [ 50%]
tests/test_telemetry_simulator.py::test_build_payload_includes_both_routers PASSED [ 75%]
tests/test_telemetry_simulator.py::test_post_metrics_sends_payload_to_write_endpoint PASSED [100%]

============================== 4 passed in 0.08s ===============================
```

**Result:** All 4 tests pass. The tests validate output shape, type, and ranges—none assert implementation details—so the RNG optimization change is transparent to the test suite.
