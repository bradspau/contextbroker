"""Upstream health for GET /health (OPS-01): is each store answering, in fixed strings only.

The result holds four keys with fixed values. It never carries a URL, hostname, port, version or
response text, because the route is public. Results are cached for a short window so that a flood
of unauthenticated requests makes at most three upstream probes per window.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests

from broker_engine import sparql_resolver, tsdb_joiner
from broker_engine.faults import log_fault
from intent_management import tmf921_client

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 2
HEALTH_CACHE_S = 5
ANSWERING = "answering"
NOT_ANSWERING = "not answering"

_lock = threading.Lock()
_cached: tuple[float, dict] | None = None


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _targets() -> list[tuple[str, str, tuple[str, str] | None]]:
    """(name, probe URL, auth) built per call from the current endpoint constants."""
    return [
        ("fuseki", _origin(sparql_resolver.FUSEKI_QUERY_ENDPOINT) + "/$/ping", sparql_resolver.fuseki_auth()),
        ("victoriametrics", _origin(tsdb_joiner.VM_QUERY_ENDPOINT) + "/health", None),
        ("tmf921", _origin(tmf921_client.TMF921_BASE_URL) + "/health", None),
    ]


def _probe(name: str, url: str, auth) -> str:
    try:
        response = requests.get(url, timeout=PROBE_TIMEOUT_S, allow_redirects=False, auth=auth)
    except Exception as exc:  # noqa: BLE001 - any fault means "not answering"
        log_fault(log, exc, f"health probe failed: {name}")
        return NOT_ANSWERING
    status = response.status_code
    if 200 <= status < 300:
        return ANSWERING
    log.warning("health probe failed: %s: HTTP %d", name, status)
    return NOT_ANSWERING


def _run_probes() -> dict:
    targets = _targets()
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = list(pool.map(lambda t: _probe(*t), targets))
    body = {"status": "ok" if all(r == ANSWERING for r in results) else "degraded"}
    body.update({name: result for (name, _, _), result in zip(targets, results)})
    return body


def probe_upstreams() -> dict:
    """The four-key health body, probing the three upstreams in parallel at most once per window."""
    global _cached
    with _lock:
        now = time.monotonic()
        if _cached is not None and 0 <= now - _cached[0] < HEALTH_CACHE_S:
            return dict(_cached[1])
        body = _run_probes()
        _cached = (time.monotonic(), body)
        return dict(body)


def _reset_cache() -> None:
    """Forget the cached result (tests)."""
    global _cached
    with _lock:
        _cached = None
