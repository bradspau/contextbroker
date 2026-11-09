"""Builders for promoted records, shared by the store and request tests (plan 07-03).

Literal rule strings are used on purpose: this module must not depend on the
promotion vocabulary module. Plan 07-04 pins the two equal.
"""
import json
from datetime import datetime, timezone

from broker_state.store import NewPromoted

SID = "sit-" + "a" * 32
REQUESTER = "alice"
VALID_FROM = datetime(2026, 11, 6, 11, 50, 0, tzinfo=timezone.utc)

CONFLICT_RULE = "repeated_link_down_vs_configured"
HYPOTHESIS_RULE = "adjacency_without_configured_link"


def EVENT(n: int) -> str:
    return "evt-" + f"{n:032x}"


def _device_of(node_id: str) -> str:
    return node_id.split("/", 1)[0]


def new_promoted(n: int = 1, *, device: str = "Router-A", valid_from=VALID_FROM,
                 situation_id: str = SID, requester: str = REQUESTER,
                 event_ids=(EVENT(1), EVENT(2), EVENT(3))) -> NewPromoted:
    """A conflict record: promoted_id prm- plus n in 32 hex digits."""
    body = json.dumps({"device": device, "event": "link_down",
                       "configured": "link_in_force",
                       "text": "event link_down contradicts configured link_in_force"},
                      sort_keys=True)
    return NewPromoted(promoted_id="prm-" + f"{n:032x}", kind="conflict",
                       rule=CONFLICT_RULE, device_a=device, device_b=None, body=body,
                       valid_from=valid_from, situation_id=situation_id,
                       situation_requester=requester, event_ids=tuple(event_ids))


def new_hypothesis(n: int, a_node: str, b_node: str, *, valid_from=VALID_FROM,
                   situation_id: str = SID, requester: str = REQUESTER,
                   event_ids=(EVENT(1), EVENT(2), EVENT(3))) -> NewPromoted:
    """A hypothesis record between two node ids; the node ids are sorted first."""
    first, second = sorted((a_node, b_node))
    body = json.dumps({"from": first, "to": second}, sort_keys=True)
    return NewPromoted(promoted_id="prm-" + f"{n:032x}", kind="hypothesis",
                       rule=HYPOTHESIS_RULE, device_a=_device_of(first),
                       device_b=_device_of(second), body=body, valid_from=valid_from,
                       situation_id=situation_id, situation_requester=requester,
                       event_ids=tuple(event_ids))
