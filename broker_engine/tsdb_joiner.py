"""Join observed telemetry from VictoriaMetrics into context envelopes."""
import os
from datetime import datetime, timezone

import requests

from broker_engine.validation import (
    REQUEST_TIMEOUT_S,
    _SAFE_WINDOW,
    ClientInputError,
    _validate_token,
)
from data_contracts.envelope import ContextEnvelope, build_observed
from ingestion.telemetry_simulator import MEASUREMENT_NAME

VM_QUERY_ENDPOINT = os.environ.get("VM_QUERY_ENDPOINT", "http://localhost:8428/api/v1/query")


def join_observed_metric(
    device: str,
    interface: str,
    metric_type: str,
    time_window: str,
    query_endpoint: str = VM_QUERY_ENDPOINT,
    now: datetime | None = None,
) -> ContextEnvelope:
    _validate_token(device, "device")
    _validate_token(interface, "interface")
    _validate_token(metric_type, "metric_type")
    if not _SAFE_WINDOW.match(time_window):
        raise ClientInputError(f"Unsafe time_window: {time_window!r}")

    selector = (
        f'{MEASUREMENT_NAME}_{metric_type}'
        f'{{device="{device}", interface="{interface}"}}'
    )
    promql = f'max(max_over_time({selector}[{time_window}]))'

    response = requests.get(query_endpoint, params={"query": promql}, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    result = response.json()["data"]["result"]

    if now is None:
        now = datetime.now(timezone.utc)
    entity_id = f"{device}:{interface}"
    if not result:
        return build_observed(entity_id, None, "victoriametrics", None, now)
    raw = result[0]["value"][1]
    observed_ts = _oldest_sample_ts(query_endpoint, selector, time_window)
    observed_at = (
        datetime.fromtimestamp(observed_ts, timezone.utc)
        if observed_ts is not None
        else None  # fail closed: build_observed marks this stale
    )
    return build_observed(entity_id, float(raw), "victoriametrics", observed_at, now)


def _oldest_sample_ts(query_endpoint: str, selector: str, time_window: str):
    """Epoch seconds of the OLDEST per-series last sample in the window, or None.

    An instant max_over_time query reports the evaluation time, not the sample
    time, so tlast_over_time (verified against VictoriaMetrics v1.102.0; the
    name last_timestamp is not a supported function) is needed to judge
    staleness. min() over series judges the worst case. Any failure -> None,
    which the caller treats as stale (fail closed).
    """
    try:
        response = requests.get(
            query_endpoint,
            params={"query": f"min(tlast_over_time({selector}[{time_window}]))"},
            timeout=REQUEST_TIMEOUT_S,
        )
        response.raise_for_status()
        result = response.json()["data"]["result"]
        if not result:
            return None
        return float(result[0]["value"][1])
    except Exception:
        return None
