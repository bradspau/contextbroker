"""Operations queries: read-only context about the network that needs no intent.

Nothing here judges compliance. Each result carries ``quality`` (stale, missing,
conflicts) so "no data" is never mistaken for "zero" or "none".
"""
import json
import math
from datetime import datetime, timedelta, timezone

import requests  # noqa: F401 -- tests patch broker_engine.operations.requests.get and .post

from broker_engine import upstream_http
from broker_engine.sparql_resolver import FUSEKI_QUERY_ENDPOINT, fuseki_auth
from broker_engine.tsdb_joiner import VM_QUERY_ENDPOINT
from broker_engine.validation import (
    SAFE_WINDOW,
    REQUEST_TIMEOUT_S,
    ClientInputError,
    _validate_token,
    tsdb_series_limit,
    window_seconds,
)
from data_contracts.envelope import DEFAULT_MAX_AGE_S
from data_contracts.fluents import iso_ms
from data_contracts.telemetry import MEASUREMENT_NAME

MAX_INTERFACES = 200
DEFAULT_INTERFACES = 50

# The one place the interfaces-nonzero query form is written. limitk keeps at most n series;
# label_del drops __name__ so all four rollups (tlast_over_time drops it on its own) rank, and
# so keep, the same series. n is always the validated integer plus one.
LIMITED_ROLLUP = 'limitk({n}, label_del({fn}({selector}), "__name__"))'
SERIES_LIMIT_CONFLICT = "series limit reached ({limit})"
_ROLLUPS = ("max_over_time", "min_over_time", "last_over_time", "tlast_over_time")


def _evaluation_time(as_of, now):
    now = now or datetime.now(timezone.utc)
    if as_of is None:
        return now, {}
    at = now - timedelta(seconds=window_seconds(as_of, "as_of"))
    return at, {"time": at.timestamp()}


def _check_window(time_window):
    if not isinstance(time_window, str) or not SAFE_WINDOW.match(time_window):
        raise ClientInputError(f"Unsafe time_window: {time_window!r}")


def _vector(endpoint, promql, extra):
    response = upstream_http.get("victoriametrics", endpoint,
                                 params={"query": promql, **extra},
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
    series_limit = tsdb_series_limit()  # read on each call

    def raw(fn):
        # one more than the limit, so a result that was cut can be told from one that was not
        return _vector(query_endpoint,
                       LIMITED_ROLLUP.format(n=series_limit + 1, fn=fn, selector=selector), extra)

    def label_key(row):
        return json.dumps(row.get("metric", {}), sort_keys=True)

    def by_interface(rows, keep):
        grouped = {}
        for r in rows:
            name = r.get("metric", {}).get("interface")
            if name and (keep is None or label_key(r) in keep):
                grouped.setdefault(name, []).append(r["value"][1])
        return grouped

    fetched = {fn: raw(fn) for fn in _ROLLUPS[:2]}
    out = {
        "device": device, "metric": metric_type, "time_window": time_window, "as_of": as_of,
        "evaluated_at": at.isoformat(), "state": "observed", "source": "victoriametrics",
        "interfaces_examined": 0, "interfaces": [], "truncated": False,
        "series_truncated": False, "stale_interfaces": 0,
    }
    if not fetched["max_over_time"] and not fetched["min_over_time"]:
        out["quality"] = _quality(missing=True)
        return out
    fetched.update({fn: raw(fn) for fn in _ROLLUPS[2:]})

    # More than the limit came back: keep the first `series_limit` label sets in sorted order,
    # the same ones in every rollup, and say so.
    keep = None
    over = next((fetched[fn] for fn in _ROLLUPS if len(fetched[fn]) > series_limit), None)
    if over is not None:
        keep = set(sorted({label_key(r) for r in over})[:series_limit])
        out["series_truncated"] = True
    maxs, mins = (by_interface(fetched[fn], keep) for fn in _ROLLUPS[:2])
    lasts, times = (by_interface(fetched[fn], keep) for fn in _ROLLUPS[2:])

    conflicts, found, stale_count = [], [], 0
    if out["series_truncated"]:
        conflicts.append(SERIES_LIMIT_CONFLICT.format(limit=series_limit))
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


def interface_neighbours(device, interface, *, query_endpoint=FUSEKI_QUERY_ENDPOINT,
                         as_of=None, known_at=None, now=None) -> dict:
    """Interfaces on the other end of every topology link this interface is on.

    Declared (configured) topology as recorded in the graph, read from the link fluents
    valid at a valid time and as known at a knowledge time. ``as_of`` ("10m") is an offset
    back from now for the valid time (now if omitted); ``known_at`` ("1h") is an offset
    back from now for the knowledge time (the latest record if omitted). An interface that
    is not in the graph is ``quality.missing``; one that is but had no link valid then has
    an empty list. ``valid_at`` and ``known_at`` appear in the result only when asked."""
    _validate_token(device, "device")
    _validate_token(interface, "interface")
    now = now or datetime.now(timezone.utc)
    valid_s = window_seconds(as_of, "as_of") if as_of is not None else 0
    known_s = window_seconds(known_at, "known_at") if known_at is not None else None
    valid = iso_ms(now - timedelta(seconds=valid_s))
    if known_s is None:
        known_filter = "FILTER(!BOUND(?sa))"
        known = None
    else:
        known = iso_ms(now - timedelta(seconds=known_s))
        known_filter = (f'FILTER(?ra <= "{known}"^^xsd:dateTime && '
                        f'(!BOUND(?sa) || "{known}"^^xsd:dateTime < ?sa))')
    sparql = f"""
    PREFIX : <http://example.org/network-ontology#>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
    SELECT ?self ?link ?label ?otherDevice ?otherName WHERE {{
      ?self :onDevice "{device}" ; :interfaceName "{interface}" .
      OPTIONAL {{
        ?lf a :LinkFluent ; :ofLink ?link ; :hasInterface ?self , ?other ;
            :validStart ?vs ; :recordedAt ?ra .
        OPTIONAL {{ ?lf :validEnd ?ve }}
        OPTIONAL {{ ?lf :supersededAt ?sa }}
        FILTER(?other != ?self)
        FILTER(?vs <= "{valid}"^^xsd:dateTime && (!BOUND(?ve) || "{valid}"^^xsd:dateTime < ?ve))
        {known_filter}
        ?other :onDevice ?otherDevice ; :interfaceName ?otherName .
        OPTIONAL {{ ?link rdfs:label ?label }}
      }}
    }}"""
    response = upstream_http.post("fuseki", query_endpoint, data={"query": sparql},
                                  headers={"Accept": "application/sparql-results+json"},
                                  auth=fuseki_auth(), timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    rows = response.json()["results"]["bindings"]
    out = {"entity_id": f"{device}:{interface}", "state": "configured", "source": "fuseki",
           "observed_at": None, "neighbours": []}
    if as_of is not None or known_at is not None:
        out["valid_at"], out["known_at"] = valid, known
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
