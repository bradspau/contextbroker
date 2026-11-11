"""Real-engine check of the limited interfaces-nonzero selection (PERF-03, D-86).

tests/test_operations.py runs the real interfaces_with_nonzero code against a fake that answers
by rollup name. This module runs the same code against a real VictoriaMetrics v1.102.0, which is
the one thing the fake cannot show: that limitk(N+1, label_del(<rollup>(<selector>), "__name__"))
returns at most N+1 series and that all four rollups choose the same series.

It is skipped unless VM_PARITY_URL is set. It must never be pointed at the persistent store:
vm_parity_target() refuses any host other than 127.0.0.1 or localhost, port 8428 (the persistent
store's published port), any URL without an explicit port, and any URL with a path, credentials,
a query or a fragment, before a single request is sent. The test never targets port 8428. Every
device name carries a random per-run suffix so repeated runs on one container do not collide, and
nothing here deletes a series.

The exact command: a throwaway container with no volume, published on loopback port 18428 only.

    docker run -d --rm --name phase8-vm -p 127.0.0.1:18428:8428 victoriametrics/victoria-metrics:v1.102.0
    VM_PARITY_URL=http://127.0.0.1:18428 venv/bin/python -m pytest -q tests/test_vm_parity.py -rs
    docker rm -f phase8-vm

VictoriaMetrics hides samples newer than about 30 s from instant queries, so the samples are
written 120 s and 60 s in the past.
"""
import os
import time
import uuid
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import requests

from broker_engine import operations as ops
from ingestion.telemetry_simulator import MEASUREMENT_NAME

URL_ENV = "VM_PARITY_URL"
PERSISTENT_PORT = 8428
TIMEOUT_S = 30
REAL_GET = requests.get  # captured before any test patches requests.get
REAL_POST = requests.post
ROLLUPS = ("max_over_time", "min_over_time", "last_over_time", "tlast_over_time")
SERIES_COUNT = 10


def vm_parity_target(url) -> str:
    """The base URL of a throwaway VictoriaMetrics, or ValueError.

    Accepts only http on 127.0.0.1 or localhost with an explicit port other than 8428 and no
    path, credentials, query or fragment. Sends nothing."""
    if not isinstance(url, str):
        raise ValueError("the parity URL must be a string")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError("not a usable parity URL") from None
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("the parity URL must be http on 127.0.0.1 or localhost")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("the parity URL must not carry credentials, a query or a fragment")
    if port is None or port == PERSISTENT_PORT:
        raise ValueError(f"the parity URL needs an explicit port other than {PERSISTENT_PORT}")
    if parts.path not in ("", "/"):
        raise ValueError("the parity URL must be a bare host and port")
    return f"http://{parts.hostname}:{port}"


# ---- the safety check itself: always runs, sends nothing ------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8428",
    "http://localhost:8428/",
    "http://127.0.0.1",
    "https://127.0.0.1:18428",
    "http://10.0.0.5:18428",
    "http://example.com:18428",
    "http://user:pw@127.0.0.1:18428",
    "http://127.0.0.1:18428/api/v1",
    "http://127.0.0.1:18428?x=1",
    "http://victoriametrics:18428",
    "",
    None,
    18428,
    b"http://127.0.0.1:18428",
])
def test_vm_parity_target_refuses_anything_but_a_loopback_throwaway(url):
    with patch("requests.get") as get, patch("requests.post") as post:
        with pytest.raises(ValueError):
            vm_parity_target(url)
    get.assert_not_called()
    post.assert_not_called()


@pytest.mark.parametrize("url, base", [
    ("http://127.0.0.1:18428", "http://127.0.0.1:18428"),
    ("http://localhost:18428/", "http://localhost:18428"),
])
def test_vm_parity_target_accepts_a_loopback_throwaway(url, base):
    assert vm_parity_target(url) == base


# ---- the real-engine test -------------------------------------------------------------------------

@pytest.fixture(scope="module")
def base():
    url = os.environ.get(URL_ENV)
    if not url:
        pytest.skip(f"real-engine test; set {URL_ENV} to a throwaway VictoriaMetrics URL")
    return vm_parity_target(url)  # raises, failing the run, for the persistent store's address


@pytest.fixture(scope="module")
def device(base):
    name = "p8" + uuid.uuid4().hex[:8]
    now = time.time_ns()
    lines = []
    for i in range(SERIES_COUNT):
        for back_s in (120, 60):
            lines.append(f"{MEASUREMENT_NAME},device={name},interface=e{i} "
                         f"input_drops={i + 1}i {now - back_s * 1_000_000_000}")
    r = REAL_POST(f"{base}/write", data="\n".join(lines).encode("utf-8"), timeout=TIMEOUT_S)
    r.raise_for_status()
    REAL_GET(f"{base}/internal/force_flush", timeout=TIMEOUT_S).raise_for_status()
    return name


def _query(base, promql):
    r = REAL_GET(f"{base}/api/v1/query", params={"query": promql}, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()["data"]["result"]


def _wait_for_series(base, device):
    selector = f'{MEASUREMENT_NAME}_input_drops{{device="{device}"}}[10m]'
    for _ in range(30):
        if len(_query(base, f"max_over_time({selector})")) == SERIES_COUNT:
            return selector
        time.sleep(1)
    pytest.fail("the written series never became readable")


def _label_sets(rows):
    return sorted(tuple(sorted(r["metric"].items())) for r in rows)


def test_limitk_with_label_del_reads_at_most_n_plus_one_and_all_rollups_agree(base, device):
    selector = _wait_for_series(base, device)
    chosen = {}
    for fn in ROLLUPS:
        rows = _query(base, ops.LIMITED_ROLLUP.format(n=5, fn=fn, selector=selector))
        assert len(rows) == 5, fn
        assert all("__name__" not in r["metric"] for r in rows), fn
        assert all({"device", "interface"} <= set(r["metric"]) for r in rows), fn
        chosen[fn] = _label_sets(rows)
    assert len({tuple(v) for v in chosen.values()}) == 1  # the same five label sets in all four


def test_a_cut_result_says_so_and_a_wide_limit_does_not(base, device, monkeypatch):
    _wait_for_series(base, device)
    endpoint = f"{base}/api/v1/query"
    monkeypatch.setenv("BROKER_TSDB_SERIES_LIMIT", "4")
    cut = ops.interfaces_with_nonzero(device, "input_drops", "10m", query_endpoint=endpoint)
    assert cut["series_truncated"] is True
    assert cut["interfaces_examined"] == 4
    assert "series limit reached (4)" in cut["quality"]["conflicts"]
    monkeypatch.setenv("BROKER_TSDB_SERIES_LIMIT", "20")
    wide = ops.interfaces_with_nonzero(device, "input_drops", "10m", query_endpoint=endpoint)
    assert wide["series_truncated"] is False
    assert wide["interfaces_examined"] == SERIES_COUNT
    assert wide["quality"]["conflicts"] == []
