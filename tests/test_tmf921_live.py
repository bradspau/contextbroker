"""Operator conformance against a live TMF921 server (skipped unless TMF921_LIVE=1).

For each metric in data_contracts/metric_map.json, push a value just below, at,
and just above the intent's bound, take the server's verdict from its newest
report (through interpret_report, as the broker does) and check that the
local evaluate_intent verdict with the map's operator agrees.

The probes are direction-free (below/at/above), so the test does not assume
the map's operator to pick "inside" and "outside": the side that is inside
depends on the operator under test. The value at the bound is what tells
"<=" from "<" (and ">=" from ">").

Each probe uses its own intent: a Degraded evaluation rewrites the failed
bound in the stored intent, so an intent is used for at most one failing
probe. The four other metrics get compliant values first (from the
simulator's HSI ranges, which do not depend on the map) and the probed metric
is posted last, so every earlier evaluation fails only with "no observation",
which leaves the bound intact (checked before the probe post).

Needs: the TMF921 server at TMF921_BASE_URL with its ontology mounted, and the
public HSI demo intent Turtle at TMF921_DEMO_INTENT_TTL (default: the
BBF_921_java checkout next to this repo). The Turtle is read at run time, not
copied into this repo. Intents created here are deleted afterwards.

    TMF921_LIVE=1 python -m pytest -v tests/test_tmf921_live.py
"""
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.skipif(os.environ.get("TMF921_LIVE") != "1",
                                reason="live TMF921 server test; set TMF921_LIVE=1")

from data_contracts.envelope import build_observed  # noqa: E402
from data_contracts.metric_map import load_metric_map  # noqa: E402
from ingestion.telemetry_simulator import HSI_COMPLIANT_RANGES  # noqa: E402
from intent_management.tmf921_client import Tmf921Client, intent_bounds  # noqa: E402
from outcome_assurance.evaluate import evaluate_intent  # noqa: E402
from outcome_assurance.tmf921_report import short_name, interpret_report  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "create_demo_intent.sh"
DEMO_TTL = Path(os.environ.get(
    "TMF921_DEMO_INTENT_TTL",
    str(REPO.parent / "BBF_921_java" / "seed_data" / "hsi_demo_intent.ttl")))
BASE = os.environ.get("TMF921_BASE_URL", "http://localhost:8000/tmf-api/intentManagement/v5")
METRIC_MAP = load_metric_map()
PROBES = ("below", "at", "above")
POLL_S, TIMEOUT_S = 0.2, 10.0


def _probe_value(bound: float, probe: str) -> float:
    delta = max(abs(bound) * 0.01, 0.001)
    return {"below": bound - delta, "at": bound, "above": bound + delta}[probe]


def _compliant(broker_metric: str) -> float:
    low, high = HSI_COMPLIANT_RANGES[broker_metric]
    return round((low + high) / 2, 3)


def _create_intent() -> str:
    if not DEMO_TTL.is_file():
        pytest.skip(f"demo intent Turtle not found: {DEMO_TTL}")
    out = subprocess.run(["bash", str(SCRIPT), str(DEMO_TTL), "operator conformance probe"],
                         capture_output=True, text=True, timeout=60,
                         env={**os.environ, "TMF921_BASE_URL": BASE})
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().rsplit(" ", 1)[-1]


def _settled_reports(client, intent_id, at_least):
    deadline = time.monotonic() + TIMEOUT_S
    while True:
        reports = client.list_reports(intent_id)
        if len(reports) >= at_least or time.monotonic() > deadline:
            return reports
        time.sleep(POLL_S)


def _newest(reports):
    return max(reports, key=lambda r: r["creationDate"])


def _key(report):
    return report.get("id") or report["creationDate"]


def _missing(report, uri):
    """True when the report says the probed metric still has no observation."""
    reason = report.get("intentHandlingReason") or ""
    return "no observation" in reason and short_name(uri) in reason


def _await_report(client, intent_id, ready):
    """Poll until a report satisfies `ready`, and return (all reports, the newest such report).

    A higher report count is not enough (bead contextbroker-2y4): the server can list a report for an
    earlier post, or one it has not finished, before the report for the post under test. The caller
    names the report it waits for by its content. The report is None when none arrived in time."""
    deadline = time.monotonic() + TIMEOUT_S
    while True:
        reports = client.list_reports(intent_id)
        ready_reports = [r for r in reports if ready(r)]
        if ready_reports:
            return reports, _newest(ready_reports)
        if time.monotonic() > deadline:
            return reports, None
        time.sleep(POLL_S)


@pytest.mark.parametrize("probe", PROBES)
@pytest.mark.parametrize("uri", sorted(METRIC_MAP), ids=lambda u: short_name(u))
def test_server_and_local_operator_agree(uri, probe):
    client = Tmf921Client(BASE)
    mapping = METRIC_MAP[uri]
    intent_id = _create_intent()
    try:
        bounds = intent_bounds(client.get_intent(intent_id))
        assert set(bounds) == set(METRIC_MAP), "demo intent bounds unreadable"
        bound = bounds[uri]
        stamps = {}

        _settled_reports(client, intent_id, 1)
        others = [u for u in sorted(METRIC_MAP) if u != uri]
        for u in others:
            stamps[u] = datetime.now(timezone.utc)
            client.post_observation(intent_id, u, _compliant(METRIC_MAP[u].broker_metric),
                                    METRIC_MAP[u].unit, stamps[u])
        # The report that follows the last compliant post: only the probed metric is missing.
        before, pre = _await_report(
            client, intent_id,
            lambda r: (r.get("intentHandlingReason") or "").count("no observation") == 1
            and _missing(r, uri))
        assert pre is not None, f"no report with only {short_name(uri)} missing: {_newest(before)}"
        assert intent_bounds(client.get_intent(intent_id))[uri] == bound
        seen = {_key(r) for r in before}

        value = _probe_value(bound, probe)
        stamps[uri] = datetime.now(timezone.utc)
        client.post_observation(intent_id, uri, value, mapping.unit, stamps[uri])
        # The report that carries the probe post: new since the post, and no longer missing the metric.
        after, report = _await_report(
            client, intent_id, lambda r: _key(r) not in seen and not _missing(r, uri))
        assert report is not None, f"no report carries the probe post: {_newest(after)}"
        now = datetime.now(timezone.utc)
        server = interpret_report(report, now=now, observed_at=stamps).outcome

        spec = {"metric": mapping.broker_metric, "operator": mapping.operator,
                "target_value": bound,
                "interfaces": [{"device": "probe", "interface_name": "probe"}]}
        env = build_observed("probe:probe", value, "probe", stamps[uri], now)
        local = evaluate_intent(spec, {("probe", "probe"): env}).outcome

        evidence = (f"{short_name(uri)} bound={bound} value={value} ({probe}); "
                    f"map operator {mapping.operator!r} -> local {local.value}; "
                    f"server {report['intentHandlingState']} "
                    f"({report.get('intentHandlingReason')!r}) -> {server.value}")
        print(evidence)
        assert server == local, "DISAGREEMENT: " + evidence
    finally:
        try:
            requests.delete(f"{BASE}/intent/{intent_id}", timeout=10)
        except requests.RequestException:
            pass
