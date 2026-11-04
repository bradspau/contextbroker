"""Operations queries: read-only context about the network that needs no intent.

Nothing here judges compliance. Each result carries ``quality`` (stale, missing,
conflicts) so "no data" is never mistaken for "zero" or "none".
"""
import math
from datetime import datetime, timedelta, timezone

import requests

from broker_engine.sparql_resolver import FUSEKI_QUERY_ENDPOINT, fuseki_auth
from broker_engine.tsdb_joiner import VM_QUERY_ENDPOINT
from broker_engine.validation import (
    _SAFE_WINDOW,
    REQUEST_TIMEOUT_S,
    ClientInputError,
    _validate_token,
    window_seconds,
)
from data_contracts.envelope import DEFAULT_MAX_AGE_S
from ingestion.telemetry_simulator import MEASUREMENT_NAME

MAX_INTERFACES = 200
DEFAULT_INTERFACES = 50


def _evaluation_time(as_of, now):
    now = now or datetime.now(timezone.utc)
    if as_of is None:
        return now, {}
    at = now - timedelta(seconds=window_seconds(as_of, "as_of"))
    return at, {"time": at.timestamp()}


def _check_window(time_window):
    if not isinstance(time_window, str) or not _SAFE_WINDOW.match(time_window):
        raise ClientInputError(f"Unsafe time_window: {time_window!r}")


def _vector(endpoint, promql, extra):
    response = requests.get(endpoint, params={"query": promql, **extra},
                            timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    return response.json()["data"]["result"]


def _number(raw):
    """float(raw) if finite, else None."""
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _sample_time(raw, at):
    """UTC datetime of a tlast_over_time value; None if unusable or in the future."""
    ts = _number(raw)
    if ts is None or ts <= 0:
        return None
    try:
        when = datetime.fromtimestamp(ts, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return None if (when - at).total_seconds() > 5 else when


def _is_stale(observed_at, at):
    return observed_at is None or (at - observed_at).total_seconds() > DEFAULT_MAX_AGE_S


def _quality(stale=False, missing=False, conflicts=None):
    return {"stale": stale, "missing": missing, "conflicts": list(conflicts or [])}


def metric_history(device, interface, metric_type, time_window, as_of=None, *,
                   query_endpoint=VM_QUERY_ENDPOINT, now=None) -> dict:
    """min / max / last and sample count of one metric over the window ending at
    ``as_of`` ago (now if omitted)."""
    _validate_token(device, "device")
    _validate_token(interface, "interface")
    _validate_token(metric_type, "metric_type")
    _check_window(time_window)
    at, extra = _evaluation_time(as_of, now)
    selector = (f'{MEASUREMENT_NAME}_{metric_type}'
                f'{{device="{device}", interface="{interface}"}}')
    window = f"[{time_window}]"
    out = {
        "entity_id": f"{device}:{interface}", "state": "observed", "source": "victoriametrics",
        "metric": metric_type, "time_window": time_window, "as_of": as_of,
        "evaluated_at": at.isoformat(),
        "min": None, "max": None, "last": None, "samples": 0, "observed_at": None,
    }
    mins = _vector(query_endpoint, f"min_over_time({selector}{window})", extra)
    if not mins:
        out["quality"] = _quality(missing=True)
        return out
    maxs = _vector(query_endpoint, f"max_over_time({selector}{window})", extra)
    lasts = _vector(query_endpoint, f"last_over_time({selector}{window})", extra)
    counts = _vector(query_endpoint, f"count_over_time({selector}{window})", extra)
    times = _vector(query_endpoint, f"min(tlast_over_time({selector}{window}))", extra)

    conflicts = []
    series = len(mins)
    if series > 1:
        conflicts.append(f"multiple series match selector ({series})")
    lows = [_number(r["value"][1]) for r in mins]
    highs = [_number(r["value"][1]) for r in maxs]
    if any(v is None for v in lows + highs):
        conflicts.append("non-finite value ignored")
    lows, highs = [v for v in lows if v is not None], [v for v in highs if v is not None]
    out["min"] = min(lows) if lows else None
    out["max"] = max(highs) if highs else None
    # "last" is only well defined for a single series.
    out["last"] = _number(lasts[0]["value"][1]) if series == 1 and lasts else None
    out["samples"] = int(sum(v for v in (_number(r["value"][1]) for r in counts) if v))
    observed_at = _sample_time(times[0]["value"][1], at) if times else None
    out["observed_at"] = observed_at.isoformat() if observed_at else None
    out["quality"] = _quality(stale=_is_stale(observed_at, at), conflicts=conflicts)
    return out


def interfaces_with_nonzero(device, metric_type, time_window, as_of=None,
                            limit=DEFAULT_INTERFACES, *,
                            query_endpoint=VM_QUERY_ENDPOINT, now=None) -> dict:
    """Interfaces of a device whose metric was non-zero at any point in the window.

    Only interfaces that reported at least once are examined. If none did,
    ``quality.missing`` is true: that is "no data", not "all zero"."""
    _validate_token(device, "device")
    _validate_token(metric_type, "metric_type")
    _check_window(time_window)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_INTERFACES:
        raise ClientInputError(f"limit must be an integer from 1 to {MAX_INTERFACES}")
    at, extra = _evaluation_time(as_of, now)
    selector = f'{MEASUREMENT_NAME}_{metric_type}{{device="{device}"}}[{time_window}]'

    def by_interface(fn):
        rows = {}
        for r in _vector(query_endpoint, f"{fn}({selector})", extra):
            name = r.get("metric", {}).get("interface")
            if name:
                rows.setdefault(name, []).append(r["value"][1])
        return rows

    maxs, mins = by_interface("max_over_time"), by_interface("min_over_time")
    out = {
        "device": device, "metric": metric_type, "time_window": time_window, "as_of": as_of,
        "evaluated_at": at.isoformat(), "state": "observed", "source": "victoriametrics",
        "interfaces_examined": 0, "interfaces": [], "truncated": False, "stale_interfaces": 0,
    }
    if not mins and not maxs:
        out["quality"] = _quality(missing=True)
        return out
    lasts, times = by_interface("last_over_time"), by_interface("tlast_over_time")

    conflicts, found, stale_count = [], [], 0
    for name in sorted(set(maxs) | set(mins)):
        if len(maxs.get(name, [])) > 1 or len(mins.get(name, [])) > 1:
            conflicts.append(f"multiple series for {name}")
        hi = [v for v in map(_number, maxs.get(name, [])) if v is not None]
        lo = [v for v in map(_number, mins.get(name, [])) if v is not None]
        if not hi or not lo:
            conflicts.append(f"non-finite value ignored for {name}")
            continue
        high, low = max(hi), min(lo)
        stamp = _sample_time(times.get(name, [None])[0], at) if times.get(name) else None
        stale = _is_stale(stamp, at)
        stale_count += stale
        found.append(name)
        if high != 0 or low != 0:
            last = _number(lasts[name][0]) if len(lasts.get(name, [])) == 1 else None
            out["interfaces"].append({
                "interface": name, "min": low, "max": high, "last": last,
                "observed_at": stamp.isoformat() if stamp else None, "stale": stale,
            })
    out["interfaces_examined"] = len(found)
    out["stale_interfaces"] = stale_count
    out["interfaces"].sort(key=lambda i: (-max(abs(i["min"]), abs(i["max"])), i["interface"]))
    if len(out["interfaces"]) > limit:
        out["interfaces"], out["truncated"] = out["interfaces"][:limit], True
    out["quality"] = _quality(stale=bool(found) and stale_count == len(found),
                              missing=not found, conflicts=conflicts)
    return out


def interface_neighbours(device, interface, *, query_endpoint=FUSEKI_QUERY_ENDPOINT) -> dict:
    """Interfaces on the other end of every topology link this interface is on.

    Declared (configured) topology, not discovered state. An interface that is not
    in the graph is ``quality.missing``; one that is but has no links has an empty
    list."""
    _validate_token(device, "device")
    _validate_token(interface, "interface")
    sparql = f"""
    PREFIX : <http://example.org/network-ontology#>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    SELECT ?link ?label ?otherDevice ?otherName WHERE {{
      ?self :onDevice "{device}" ; :interfaceName "{interface}" .
      OPTIONAL {{
        ?link :hasInterface ?self , ?other .
        FILTER(?other != ?self)
        ?other :onDevice ?otherDevice ; :interfaceName ?otherName .
        OPTIONAL {{ ?link rdfs:label ?label }}
      }}
    }}"""
    response = requests.post(query_endpoint, data={"query": sparql},
                             headers={"Accept": "application/sparql-results+json"},
                             auth=fuseki_auth(), timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    rows = response.json()["results"]["bindings"]
    out = {"entity_id": f"{device}:{interface}", "state": "configured", "source": "fuseki",
           "observed_at": None, "neighbours": []}
    if not rows:
        out["quality"] = _quality(missing=True)
        return out
    seen = set()
    for row in rows:
        if "otherDevice" not in row or "otherName" not in row:
            continue
        item = {
            "device": row["otherDevice"]["value"], "interface": row["otherName"]["value"],
            "link": row.get("label", {}).get("value") or row.get("link", {}).get("value"),
        }
        key = (item["device"], item["interface"], item["link"])
        if key not in seen:
            seen.add(key)
            out["neighbours"].append(item)
    out["neighbours"].sort(key=lambda n: (n["device"], n["interface"]))
    out["quality"] = _quality()
    return out
