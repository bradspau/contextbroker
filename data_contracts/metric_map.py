"""Metric map: broker metric names <-> TMF921 metric URIs (+ unit).

Data lives in metric_map.json (override with env METRIC_MAP_PATH). Format:

    [{"broker_metric": "latency_ms",
      "metric_uri": "http://broadband-forum.org/Intent#LatencyMetric",
      "unit": "ms", "operator": "<"}, ...]

`operator` (one of < <= == >= >) is the direction in which the metric's bound is
met: the TMF921 intent carries only the bound value (intent_bounds), so the
temporal TMF921 assess takes the direction from here. The current values
(latency, jitter, packet loss "<", strict; bandwidths ">=") were verified against
the live TMF921 server on 2026-10-28 (tests/test_tmf921_live.py: a value exactly
at a quan:smaller bound fails on the server).

Only URIs, units and operators are stored, no ontology text. The five BBF HSI metrics use
new broker metric names (emitted by the telemetry simulator with --hsi):
downstream_bandwidth_mbps, upstream_bandwidth_mbps, latency_ms, jitter_ms,
packet_loss_percent. A missing file yields an empty map; any malformed content,
including an entry without a known operator, makes load_metric_map raise
ValueError (the metric map fails closed).
"""
import json
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from broker_engine.validation import SAFE_TOKEN

URI_RE = re.compile(r"^https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+\Z")
ALLOWED_UNITS = frozenset({"Mbps", "Gbps", "ms", "us", "percent"})
ALLOWED_OPERATORS = frozenset({"<", "<=", "==", ">=", ">"})
DEFAULT_PATH = Path(__file__).with_name("metric_map.json")


@dataclass(frozen=True)
class MetricMapping:
    broker_metric: str
    metric_uri: str
    unit: str | None
    # Always set by load_metric_map. None only when built directly without one;
    # the temporal TMF921 assess then reports the metric as inconclusive.
    operator: str | None = None


def _check_token(value, label: str) -> str:
    if not isinstance(value, str) or not SAFE_TOKEN.match(value):
        raise ValueError(f"unsafe {label}: {value!r}")
    return value


def check_uri(value, label: str = "metric_uri") -> str:
    if not isinstance(value, str) or not URI_RE.match(value):
        raise ValueError(f"unsafe {label}: {value!r}")
    return value


def _resolve(path) -> Path:
    if path is None:
        path = Path(os.environ.get("METRIC_MAP_PATH") or DEFAULT_PATH)
    return Path(path)


def metric_map_key(path: Path | None = None) -> tuple:
    """Identity of the metric map file as it is now (D-72).

    (resolved path as text, inode, size, modification time in ns) from one
    os.stat, or (resolved path as text, None) when the file does not exist. A
    rewritten or replaced file has a different key; a missing one has its own.
    """
    resolved = _resolve(path).resolve()
    try:
        st = os.stat(resolved)
    except (FileNotFoundError, NotADirectoryError):
        return (str(resolved), None)
    return (str(resolved), st.st_ino, st.st_size, st.st_mtime_ns)


# Only successful loads are kept, so a malformed file raises on every call.
_CACHE_MAX = 16
_cache: "OrderedDict[tuple, dict[str, MetricMapping]]" = OrderedDict()
_cache_lock = threading.Lock()


def clear_metric_map_cache() -> None:
    with _cache_lock:
        _cache.clear()


def load_metric_map(path: Path | None = None) -> dict[str, MetricMapping]:
    path = _resolve(path)
    # The key is taken before the read: if the file changes in between, the map
    # is filed under the older key, which the next call no longer matches.
    key = metric_map_key(path)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return dict(hit)
    if len(key) == 2:  # the file does not exist
        return {}
    out = _parse(path)
    with _cache_lock:
        _cache[key] = out
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return dict(out)


def _parse(path: Path) -> dict[str, MetricMapping]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read metric map {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError("metric map must be a JSON list")
    out: dict[str, MetricMapping] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("metric map entries must be objects")
        uri = check_uri(item.get("metric_uri"))
        broker = _check_token(item.get("broker_metric"), "broker_metric")
        unit = item.get("unit")
        if unit is not None and (not isinstance(unit, str) or unit not in ALLOWED_UNITS):
            raise ValueError(f"unit not allowed: {unit!r}")
        operator = item.get("operator")
        if not isinstance(operator, str) or operator not in ALLOWED_OPERATORS:
            raise ValueError(f"operator missing or not allowed: {operator!r}")
        if uri in out:
            raise ValueError(f"duplicate metric_uri: {uri}")
        if any(m.broker_metric == broker for m in out.values()):
            raise ValueError(f"duplicate broker_metric: {broker}")
        out[uri] = MetricMapping(broker, uri, unit, operator)
    return out


def broker_metric_for(uri: str) -> MetricMapping | None:
    return load_metric_map().get(uri)
