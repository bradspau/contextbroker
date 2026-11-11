"""Upstream health for GET /health (OPS-01): is each store answering, in fixed strings only.

The result holds eight keys with fixed values: "status" ("ok" or "degraded") and "fuseki",
"victoriametrics", "tmf921", "qdrant" and "state", each "answering" or "not answering". "qdrant" is the
knowledge store (D-35), probed at the origin of BROKER_QDRANT_URL plus /readyz with no credential. "state"
is the durable state store (D-04). "evictions" is {"outcomes": n, "marks": n, "situations": n, "events": n} with
whole-number counts of the entries each state table has evicted (D-12). "events" is {"duplicates": n,
"unmapped": n, "quarantined": {"no priority": n, "not text": n, "too long": n, "unparsable header": n}}:
whole-number counts of event lines dropped as repeats, events stored without a device, and lines
quarantined for each of four fixed reasons (D-22). "evictions" and "events" are null when the state
store does not answer. They are the same public exposure class as the eviction counters: whole numbers
and fixed keys only, never a host, device, line text, path or server text. The body never carries a
URL, hostname, port, version, path or response text, because the route is public. Results are cached for
a short window so that a flood of unauthenticated requests makes at most four upstream probes and one
state ping, one eviction read and one event counter read per window.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests

from broker_engine import sparql_resolver, tsdb_joiner, upstream_http
from broker_engine.faults import log_fault
from broker_state.process import get_store
from broker_state.store import EVICTION_TABLES, QUARANTINE_REASONS
from intent_management import tmf921_client
from okf_retrieval import config as okf_config

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


def _qdrant_probe_url() -> "str | ValueError":
    """The Qdrant probe URL from the current setting, or the ValueError of a malformed setting (the
    probe then reports "not answering" and logs the class, so /health itself never crashes)."""
    try:
        return _origin(okf_config.qdrant_url()) + "/readyz"
    except ValueError as exc:
        return exc


def _targets() -> list[tuple[str, "str | ValueError", tuple[str, str] | None]]:
    """(name, probe URL, auth) built per call from the current endpoint constants and settings."""
    return [
        ("fuseki", _origin(sparql_resolver.FUSEKI_QUERY_ENDPOINT) + "/$/ping", sparql_resolver.fuseki_auth()),
        ("victoriametrics", _origin(tsdb_joiner.VM_QUERY_ENDPOINT) + "/health", None),
        ("tmf921", _origin(tmf921_client.TMF921_BASE_URL) + "/health", None),
        ("qdrant", _qdrant_probe_url(), None),
    ]


def _probe(name: str, url: "str | ValueError", auth) -> str:
    if isinstance(url, ValueError):
        log_fault(log, url, f"health probe failed: {name}")
        return NOT_ANSWERING
    try:
        if name in upstream_http.UPSTREAMS:
            response = upstream_http.get(name, url, timeout=PROBE_TIMEOUT_S,
                                         allow_redirects=False, auth=auth)
        else:  # Qdrant is not one of the three upstreams that share a session
            response = requests.get(url, timeout=PROBE_TIMEOUT_S, allow_redirects=False, auth=auth)
    except Exception as exc:  # noqa: BLE001 - any fault means "not answering"
        log_fault(log, exc, f"health probe failed: {name}")
        return NOT_ANSWERING
    status = response.status_code
    if 200 <= status < 300:
        return ANSWERING
    log.warning("health probe failed: %s: HTTP %d", name, status)
    return NOT_ANSWERING


def _probe_state() -> tuple[str, dict | None, dict | None]:
    """(state result, eviction counts or None, event counts or None). The ping and both counter reads
    are one cached probe, so a flood of requests cannot starve the store lock. The class of a fault is
    logged, never its text."""
    try:
        store = get_store()
        store.ping()
        counts = store.evictions()
        evictions = {name: int(counts[name]) for name in EVICTION_TABLES}
        raw = store.event_counters()
        events = {
            "duplicates": int(raw["duplicates"]),
            "unmapped": int(raw["unmapped"]),
            "quarantined": {reason: int(raw["quarantined"][reason]) for reason in QUARANTINE_REASONS},
        }
    except Exception as exc:  # noqa: BLE001 - any fault means "not answering"
        log_fault(log, exc, "health probe failed: state")
        return NOT_ANSWERING, None, None
    return ANSWERING, evictions, events


def _run_probes() -> dict:
    targets = _targets()
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = list(pool.map(lambda t: _probe(*t), targets))
    state, evictions, events = _probe_state()
    body = {"status": "ok" if all(r == ANSWERING for r in [*results, state]) else "degraded"}
    body.update({name: result for (name, _, _), result in zip(targets, results)})
    body["state"] = state
    body["evictions"] = evictions
    body["events"] = events
    return body


def probe_upstreams() -> dict:
    """The health body, probing the upstreams in parallel and the state store at most once per window."""
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
