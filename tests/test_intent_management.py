from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from broker_engine import main
from intent_management.store import IntentStore
from outcome_assurance.evaluate import Outcome
from tests.test_broker_api import INTENT, fake_join

client = TestClient(main.app)
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


def test_assess_reports_status_and_last_outcome():
    from broker_engine import assess as assess_mod
    assess_mod.DEFAULT_STORE._outcomes.clear()
    body = {"intent_id": "I-LO", "time_window": "5m", "source": "graph"}
    with patch("broker_engine.main.resolve_intent", return_value=INTENT), \
         patch("broker_engine.main.join_observed_metric", side_effect=fake_join):
        first = client.post("/context/assess", json=body).json()
        second = client.post("/context/assess", json=body).json()
    assert first["intent_status"] == "active" and first["last_outcome"] is None
    assert second["last_outcome"] == first["verdict"]


def test_unknown_intent_short_circuits_inconclusive():
    def join(*a, **k):
        raise AssertionError("must not join")
    with patch("broker_engine.main.resolve_intent", return_value={"metric": "latency_ms"}), \
         patch("broker_engine.main.join_observed_metric", side_effect=join):
        body = client.post("/context/assess", json={"intent_id": "I2", "time_window": "5m", "source": "graph"}).json()
    assert body["verdict"] == "inconclusive"
    assert body["intent_status"] == "unknown"
    assert "intent definition incomplete" in body["reasons"]
