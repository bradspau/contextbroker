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
    promql = f'max_over_time({selector}[{time_window}])'

    response = requests.get(query_endpoint, params={"query": promql}, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    result = response.json()["data"]["result"]

    if now is None:
        now = datetime.now(timezone.utc)
    entity_id = f"{device}:{interface}"
    if not result:
        return build_observed(entity_id, None, "victoriametrics", None, now)
    ts, raw = result[0]["value"]
    observed_ts = _last_sample_ts(query_endpoint, selector, time_window)
    if observed_ts is None:
        observed_ts = ts
    return build_observed(
        entity_id,
        float(raw),
        "victoriametrics",
        datetime.fromtimestamp(observed_ts, timezone.utc),
        now,
    )


def _last_sample_ts(query_endpoint: str, selector: str, time_window: str):
    """Epoch seconds of the newest sample in the window, or None if unavailable.

    An instant max_over_time query reports the evaluation time, not the sample
    time, so last_timestamp() is needed to judge staleness.
    """
    try:
        response = requests.get(
            query_endpoint,
            params={"query": f"last_timestamp({selector}[{time_window}])"},
            timeout=REQUEST_TIMEOUT_S,
        )
        response.raise_for_status()
        result = response.json()["data"]["result"]
        if not result:
            return None
        return float(result[0]["value"][1])
    except Exception:
        return None
