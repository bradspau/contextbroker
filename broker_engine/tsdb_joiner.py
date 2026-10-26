"""Join observed telemetry from VictoriaMetrics into context envelopes."""
from datetime import datetime, timezone

import requests

from broker_engine.validation import _SAFE_WINDOW, _validate_token
from data_contracts.envelope import ContextEnvelope, build_observed
from telemetry_simulator import MEASUREMENT_NAME

VM_QUERY_ENDPOINT = "http://localhost:8428/api/v1/query"


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
        raise ValueError(f"Unsafe time_window: {time_window!r}")

    promql = (
        f'max_over_time({MEASUREMENT_NAME}_{metric_type}'
        f'{{device="{device}", interface="{interface}"}}[{time_window}])'
    )

    response = requests.get(query_endpoint, params={"query": promql})
    response.raise_for_status()
    result = response.json()["data"]["result"]

    if now is None:
        now = datetime.now(timezone.utc)
    entity_id = f"{device}:{interface}"
    if not result:
        return build_observed(entity_id, None, "victoriametrics", None, now)
    ts, raw = result[0]["value"]
    return build_observed(
        entity_id,
        float(raw),
        "victoriametrics",
        datetime.fromtimestamp(ts, timezone.utc),
        now,
    )
