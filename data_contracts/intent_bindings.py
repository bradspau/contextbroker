"""Per-intent bindings: which device/interface supplies each metric, and the
bound the intent is expected to carry.

Data lives in intent_bindings.json (override with env INTENT_BINDINGS_PATH);
it ships as `{}`. Format: intent id -> metric URI -> {device, interface, bound}.

    {
      "hsi-demo-1": {
        "http://broadband-forum.org/Intent#LatencyMetric":
            {"device": "olt1", "interface": "pon0/1", "bound": 25.0},
        "http://broadband-forum.org/Intent#DownstreamBandwidthMetric":
            {"device": "olt1", "interface": "pon0/1", "bound": 100.0}
      }
    }

Exactly one source per metric URI per intent (JSON object keys make a second
one unrepresentable; a duplicate key is rejected explicitly). `bound` is the
value pinned at binding time: the TMF921 server rewrites a stored bound when
it evaluates Degraded, so the source compares the intent's current bound with
this pinned value. It must be a finite number (not bool, NaN or inf).
A missing file gives no bindings; malformed content raises ValueError.
"""
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

from data_contracts.metric_map import _check_token, check_uri

DEFAULT_PATH = Path(__file__).with_name("intent_bindings.json")


@dataclass(frozen=True)
class IntentBinding:
    intent_id: str
    sources: dict[str, tuple[str, str]]
    bounds: dict[str, float]


def _no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"duplicate key in bindings: {k!r}")
        d[k] = v
    return d


def _bound(value, uri: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"bound for {uri} must be a number")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"bound for {uri} is out of range") from exc
    if not math.isfinite(value):
        raise ValueError(f"bound for {uri} must be finite")
    return value


def load_bindings(path: Path | None = None) -> dict[str, IntentBinding]:
    if path is None:
        path = Path(os.environ.get("INTENT_BINDINGS_PATH") or DEFAULT_PATH)
    path = Path(path)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_dupes)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read bindings {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("bindings must be a JSON object")
    out: dict[str, IntentBinding] = {}
    for intent_id, metrics in raw.items():
        _check_token(intent_id, "intent_id")
        if not isinstance(metrics, dict):
            raise ValueError(f"bindings for {intent_id} must be an object")
        sources: dict[str, tuple[str, str]] = {}
        bounds: dict[str, float] = {}
        for uri, spec in metrics.items():
            check_uri(uri)
            if not isinstance(spec, dict):
                raise ValueError(f"binding for {uri} must be an object")
            if "bound" not in spec:
                raise ValueError(f"binding for {uri} is missing a bound")
            sources[uri] = (_check_token(spec.get("device"), "device"),
                            _check_token(spec.get("interface"), "interface"))
            bounds[uri] = _bound(spec["bound"], uri)
        out[intent_id] = IntentBinding(intent_id, sources, bounds)
    return out
