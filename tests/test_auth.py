"""REST authentication: default-deny bearer tokens on /context/*, with scopes (ACC-01)."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from broker_engine import main
from tests.auth_helpers import auth_headers, make_token

METRIC = {"device": "r1", "interface": "e0", "metric_type": "latency_ms", "time_window": "5m"}
NEIGHBOURS = {"device": "r1", "interface": "e0"}
DENIED = {"detail": "authentication required"}
SCOPE_DENIED = {"detail": "insufficient scope"}

client = TestClient(main.app)


# --- tracer ---

def test_no_token_is_401_and_reaches_no_upstream():
    with patch("requests.get") as get, patch("requests.post") as post:
        r = client.get("/context/metric", params=METRIC)
    assert r.status_code == 401
    assert r.json() == DENIED
    assert r.headers["www-authenticate"] == "Bearer"
    get.assert_not_called()
    post.assert_not_called()


def test_valid_token_reaches_the_route():
    with patch("broker_engine.main.operations.interface_neighbours", return_value={"ok": 1}) as nb:
        r = client.get("/context/neighbours", params=NEIGHBOURS, headers=auth_headers("context:read"))
    assert r.status_code == 200 and r.json() == {"ok": 1}
    nb.assert_called_once()


def test_read_only_token_cannot_assess():
    with patch("broker_engine.assess.assess_via_tmf921") as via, \
            patch("intent_management.tmf921_client.Tmf921Client.post_observation") as push:
        r = client.post("/context/assess", json={"intent_id": "I1", "time_window": "5m"},
                        headers=auth_headers("context:read"))
    assert r.status_code == 403
    assert r.json() == SCOPE_DENIED
    via.assert_not_called()
    push.assert_not_called()
