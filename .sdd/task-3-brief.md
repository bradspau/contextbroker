### Task 3: Telemetry Simulation Pipeline

**Files:**
- Create: `telemetry_simulator.py`
- Test: `tests/test_telemetry_simulator.py`

**Interfaces:**
- Produces: metric names `interface_metrics_in_octets`, `interface_metrics_out_octets`, `interface_metrics_input_drops` with labels `device` and `interface` (VictoriaMetrics derives these names/labels automatically from the InfluxDB line-protocol measurement `interface_metrics` and its tags) — a later task's PromQL depends on this exact naming, so the line-protocol measurement name must be exactly `interface_metrics` and the tag keys must be exactly `device` and `interface`.

**Global constraints for this task:**
- **Do not use git.** No `git add`/`git commit` — just create/edit the local files.
- Devices/interfaces to simulate: `Router-A` (`GigabitEthernet0/0/0`) and `Router-B` (`GigabitEthernet0/0/1`).
- Metric fields: `in_octets`, `out_octets`, `input_drops`.
- Write endpoint: `http://localhost:8428/write`. Post interval: 5 seconds.
- venv already exists at `contextbroker/venv/` with `requests`, `pytest` already installed — reuse it (`source venv/bin/activate` or run `venv/bin/pytest` directly). No new runtime dependency is needed for this task (only `requests` and stdlib `random`/`time`/`argparse`).

- [ ] **Step 1: Write failing tests for line-protocol formatting and metric generation**

Create `tests/test_telemetry_simulator.py`:

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

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_telemetry_simulator.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'telemetry_simulator'`.

- [ ] **Step 3: Write telemetry_simulator.py**

Create `telemetry_simulator.py`:

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

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_telemetry_simulator.py -v
```

Expected: PASS (4 tests).

- [ ] **Step 5: Note the manual-Docker-verification step in your report**

This step needs a real VictoriaMetrics container (not available in this sandbox) — do not attempt it, just note in your report that it's deferred:

```bash
docker compose up -d
source venv/bin/activate
python telemetry_simulator.py --once
curl -s 'http://localhost:8428/api/v1/query?query=interface_metrics_in_octets'
```

To run continuously in the background (also just document this, don't run it):

```bash
nohup python telemetry_simulator.py > telemetry.log 2>&1 &
echo $! > telemetry.pid
```

Do NOT run a "Commit" step — this project is not using git. Skip any git commands entirely.
