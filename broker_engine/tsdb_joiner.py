"""Join observed telemetry from VictoriaMetrics into context envelopes."""
import logging
import math
import os
from datetime import datetime, timedelta, timezone

import requests  # noqa: F401 -- tests patch broker_engine.tsdb_joiner.requests.get

from broker_engine import upstream_http
from broker_engine.faults import log_fault
from broker_engine.validation import (
    REQUEST_TIMEOUT_S,
    _SAFE_WINDOW,
    ClientInputError,
    _validate_token,
    window_seconds,
)
from data_contracts.envelope import ContextEnvelope, ProvenanceRecord, build_observed
from data_contracts.telemetry import MEASUREMENT_NAME

log = logging.getLogger(__name__)

VM_QUERY_ENDPOINT = os.environ.get("VM_QUERY_ENDPOINT", "http://localhost:8428/api/v1/query")


def join_observed_metric(
    device: str,
    interface: str,
    metric_type: str,
    time_window: str,
    query_endpoint: str = VM_QUERY_ENDPOINT,
    now: datetime | None = None,
    aggregate: str = "max",
    as_of: str | None = None,
) -> ContextEnvelope:
    """`aggregate` picks the window value: "max" (default, max_over_time),
    "min", or "last" (last_over_time: the latest sample, which is what
    observed_at, the time of the last sample, describes).

    `as_of` ("10m") evaluates the window as it stood that long ago: the query
    runs at now - as_of and freshness is judged against that instant, not now.

    The envelope carries one ProvenanceRecord: source "victoriametrics",
    reference the series name, observed_at the envelope's sample time and
    retrieved_at the caller's now (when the broker read it), even when as_of
    moves the query instant into the past."""
    _validate_token(device, "device")
    _validate_token(interface, "interface")
    _validate_token(metric_type, "metric_type")
    if not _SAFE_WINDOW.match(time_window):
        raise ClientInputError(f"Unsafe time_window: {time_window!r}")

    if aggregate not in ("max", "min", "last"):
        raise ClientInputError(f"Unsafe aggregate: {aggregate!r}")

    if now is None:
        now = datetime.now(timezone.utc)
    retrieved = now
    params_extra: dict = {}
    if as_of is not None:
        now = now - timedelta(seconds=window_seconds(as_of, "as_of"))
        params_extra = {"time": now.timestamp()}

    series = f"{MEASUREMENT_NAME}_{metric_type}"
    selector = f'{series}{{device="{device}", interface="{interface}"}}'
    promql = f'{aggregate}_over_time({selector}[{time_window}])'

    response = upstream_http.get("victoriametrics", query_endpoint,
                                 params={"query": promql, **params_extra},
                                 timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    result = response.json()["data"]["result"]

    entity_id = f"{device}:{interface}"
    value = None
    if result:
        try:
            value = float(result[0]["value"][1])
        except (TypeError, ValueError, OverflowError):
            value = None
    if value is None or not math.isfinite(value):
        # Empty, unreadable or non-finite: there is no sample to date, so it is missing.
        env = build_observed(entity_id, None, "victoriametrics", None, now)
    else:
        observed_at = _oldest_sample_time(query_endpoint, selector, time_window, now, params_extra)
        env = build_observed(entity_id, value, "victoriametrics", observed_at, now)
    env.provenance.append(
        ProvenanceRecord("victoriametrics", series, env.observed_at, retrieved))
    if len(result) > 1:
        # Which series is "the" value is ambiguous (the right worst case depends on
        # the intent operator), so fail closed: the evaluator treats conflicts as
        # inconclusive rather than letting one series hide another.
        env.quality.conflicts.append(
            f"multiple series match selector ({len(result)})"
        )
    return env


def query_tsdb_metrics(
    device: str,
    interface: str,
    metric_type: str,
    time_window: str,
    query_endpoint: str = VM_QUERY_ENDPOINT,
) -> dict:
    """Full context envelope (value plus quality flags), never a bare number."""
    return join_observed_metric(
        device, interface, metric_type, time_window, query_endpoint
    ).to_dict()


FUTURE_TOLERANCE_S = 5


def _oldest_sample_time(
    query_endpoint: str, selector: str, time_window: str, now: datetime,
    params_extra: dict | None = None,
) -> datetime | None:
    """Time of the OLDEST per-series last sample in the window, or None.

    An instant max_over_time query reports the evaluation time, not the sample
    time, so tlast_over_time (verified against VictoriaMetrics v1.102.0; the
    name last_timestamp is not a supported function) is needed to judge
    staleness. min() over series judges the worst case. Any failure, a
    non-finite or non-positive timestamp, or one in the future -> None, which
    the caller treats as stale (fail closed).
    """
    try:
        response = upstream_http.get("victoriametrics", query_endpoint,
                                     params={"query": f"min(tlast_over_time({selector}[{time_window}]))",
                                             **(params_extra or {})},
                                     timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
        result = response.json()["data"]["result"]
        if not result:
            return None
        ts = float(result[0]["value"][1])
        if not math.isfinite(ts) or ts <= 0:
            return None
        observed_at = datetime.fromtimestamp(ts, timezone.utc)
        if (observed_at - now).total_seconds() > FUTURE_TOLERANCE_S:
            return None
        return observed_at
    except Exception as exc:
        log_fault(log, exc, "sample time query failed")
        return None
