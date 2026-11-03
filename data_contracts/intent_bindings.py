"""One binding per TMF921 intent: which device and interface supply each metric,
and the goal pinned when the intent was bound.

Bindings live only as `:ServiceBinding` nodes in the network graph. They are
read by broker_engine/binding_resolver.py and written by
`python -m ingestion.bind_intent`. There is no file fallback.

`sources` maps a metric URI to its (device, interface); `bounds` maps the same
URI to the goal pinned at binding time (the field keeps its stored name). The
TMF921 server rewrites a stored goal when it evaluates Degraded, so the source
compares the intent's current goal with this pinned value.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class IntentBinding:
    intent_id: str
    sources: dict[str, tuple[str, str]]
    bounds: dict[str, float]
