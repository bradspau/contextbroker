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
    parser.add_argument(
        "--once", action="store_true", help="Send a single batch and exit (for manual verification)"
    )
    parser.add_argument(
        "--inject-drops",
        action="store_true",
        help="Randomly inject nonzero input_drops (5%% chance per sample) to demo the non-compliant path",
    )
    args = parser.parse_args()
    run(once=args.once, inject_drops=args.inject_drops)
