"""One binding per TMF921 intent: which device and interface supply each metric,
and the goal pinned when the intent was bound.

Bindings live only as `:ServiceBinding` nodes in the network graph, with their goals
and placements as fluents (data_contracts/fluents.py). They are read by
broker_engine/binding_resolver.py and written by `python -m ingestion.bind_intent`.
There is no file fallback.

An IntentBinding is the selection in force at one valid time and one knowledge
time. `sources` maps a metric URI to its (device, interface); `bounds` maps the same
URI to the goal pinned at binding time (the field keeps its stored name). Both hold
only the metrics that have a goal and a placement in force. `goal_intervals` and
`placement_intervals` give, for those metrics, the (start, end or None) valid
interval of the selected fluent; an end of None means still open.

`gaps` lists (metric URI, "goal" | "placement") for every metric the broker recorded
no goal or no placement for at that time. A gap is not an error: the assess
returns inconclusive for the whole intent and names the metric, so a part of an
intent is never judged as if it were the whole.

The TMF921 server rewrites a stored goal when it evaluates Degraded, so the source
compares the intent's current goal with this pinned value.
"""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class IntentBinding:
    intent_id: str
    sources: dict[str, tuple[str, str]]
    bounds: dict[str, float]
    goal_intervals: dict[str, tuple[datetime, datetime | None]] = field(default_factory=dict)
    placement_intervals: dict[str, tuple[datetime, datetime | None]] = field(default_factory=dict)
    gaps: tuple[tuple[str, str], ...] = ()
