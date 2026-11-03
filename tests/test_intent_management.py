import pytest

from intent_management.store import IntentStore
from outcome_assurance.evaluate import Outcome

FULL = {"metric": "latency_ms", "operator": "<", "target_value": 50.0,
        "interfaces": [{"device": "r1", "interface_name": "e0"}]}


def test_get_wraps_resolver_and_sets_active():
    store = IntentStore(lambda i: dict(FULL))
    intent = store.get("I1")
    assert intent.id == "I1" and intent.status == "active"
    assert intent.metric == "latency_ms" and intent.last_outcome is None
    assert intent.as_resolver_dict()["target_value"] == 50.0


def test_incomplete_dict_is_unknown():
    store = IntentStore(lambda i: {"metric": "latency_ms"})
    assert store.get("I1").status == "unknown"


def test_resolver_exception_propagates():
    def boom(i):
        raise ValueError("nope")
    with pytest.raises(ValueError):
        IntentStore(boom).get("I1")


def test_fulfilment_update_sets_last_outcome():
    store = IntentStore(lambda i: dict(FULL))
    store.fulfilment_update("I1", Outcome.FAILURE)
    assert store.get("I1").last_outcome == Outcome.FAILURE


def test_with_resolver_shares_record():
    a = IntentStore(lambda i: dict(FULL))
    b = a.with_resolver(lambda i: dict(FULL))
    b.fulfilment_update("I1", Outcome.SUCCESS)
    assert a.get("I1").last_outcome == Outcome.SUCCESS

