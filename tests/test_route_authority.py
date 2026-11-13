"""The raw operations routes and POST /context/assess honour the token's authority claim (bead 8is).

Each route refuses a token without the claim (403 `authority claims required`) and a device or class
outside it (403 `outside authority scope: entity|class`), before any Fuseki, VictoriaMetrics or
TMF921 call. /context/assess tests the devices an intent is placed on once the binding is read."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from broker_engine import main
from data_contracts.intent_bindings import IntentBinding
from tests.auth_helpers import FULL_AUTHORITY, make_token

client = TestClient(main.app)

CLAIMS_REQUIRED = {"detail": "authority claims required"}
ENTITY_REFUSED = {"detail": "outside authority scope: entity"}
CLASS_REFUSED = {"detail": "outside authority scope: class"}

# route -> (query, handler to patch, the class the route reads)
ROUTES = {
    "/context/metric": (
        {"device": "Router-A", "interface": "e0", "metric_type": "latency_ms", "time_window": "5m"},
        "broker_engine.main.query_tsdb_metrics", "observations"),
    "/context/metric-history": (
        {"device": "Router-A", "interface": "e0", "metric_type": "input_drops", "time_window": "5m"},
        "broker_engine.main.operations.metric_history", "observations"),
    "/context/interfaces-nonzero": (
        {"device": "Router-A", "metric_type": "input_drops", "time_window": "1h"},
        "broker_engine.main.operations.interfaces_with_nonzero", "observations"),
    "/context/neighbours": (
        {"device": "Router-A", "interface": "e0"},
        "broker_engine.main.operations.interface_neighbours", "topology"),
}
ASSESS_BODY = {"intent_id": "I1", "time_window": "5m"}


def _headers(scopes=("context:read", "context:assess"), **authority):
    claim = None if authority.get("none") else {**FULL_AUTHORITY, **authority}
    return {"Authorization": "Bearer " + make_token(scopes, authority=claim)}


@pytest.fixture
def no_upstream():
    with patch("requests.get") as get, patch("requests.post") as post, \
            patch("intent_management.tmf921_client.Tmf921Client.get_intent") as intent, \
            patch("intent_management.tmf921_client.Tmf921Client.post_observation") as push:
        yield get, post, intent, push


def _nothing_called(mocks):
    for mocked in mocks:
        mocked.assert_not_called()


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_a_raw_route_refuses_a_token_without_the_authority_claim(path, no_upstream):
    query, handler, _cls = ROUTES[path]
    with patch(handler) as run:
        r = client.get(path, params=query, headers=_headers(none=True))
    assert r.status_code == 403 and r.json() == CLAIMS_REQUIRED
    run.assert_not_called()
    _nothing_called(no_upstream)


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_a_raw_route_refuses_a_device_outside_the_entities(path, no_upstream):
    query, handler, _cls = ROUTES[path]
    with patch(handler) as run:
        r = client.get(path, params=query, headers=_headers(entities=["Router-B"]))
    assert r.status_code == 403 and r.json() == ENTITY_REFUSED
    run.assert_not_called()
    _nothing_called(no_upstream)


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_a_raw_route_refuses_a_token_without_the_class_it_reads(path, no_upstream):
    query, handler, cls = ROUTES[path]
    other = [c for c in FULL_AUTHORITY["classes"] if c != cls]
    with patch(handler) as run:
        r = client.get(path, params=query, headers=_headers(classes=other))
    assert r.status_code == 403 and r.json() == CLASS_REFUSED
    run.assert_not_called()
    _nothing_called(no_upstream)


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_a_raw_route_answers_for_a_covered_device_and_class(path):
    query, handler, cls = ROUTES[path]
    with patch(handler, return_value={"ok": 1}) as run:
        r = client.get(path, params=query, headers=_headers(entities=["Router-A"], classes=[cls]))
    assert r.status_code == 200 and r.json() == {"ok": 1}
    run.assert_called_once()


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_the_scope_is_still_refused_first(path, no_upstream):
    query, handler, _cls = ROUTES[path]
    with patch(handler) as run:
        r = client.get(path, params=query, headers={"Authorization": "Bearer " + make_token(
            ("context:assess",), authority=None)})
    assert r.status_code == 403 and r.json() == {"detail": "insufficient scope"}
    run.assert_not_called()


def test_assess_refuses_a_token_without_the_authority_claim(no_upstream):
    with patch("intent_management.tmf921_source.resolve_binding") as resolve:
        r = client.post("/context/assess", json=ASSESS_BODY, headers=_headers(none=True))
    assert r.status_code == 403 and r.json() == CLAIMS_REQUIRED
    resolve.assert_not_called()
    _nothing_called(no_upstream)


@pytest.mark.parametrize("classes", [["goals"], ["observations"], ["topology"]])
def test_assess_refuses_a_token_without_the_goals_and_observations_classes(classes, no_upstream):
    with patch("intent_management.tmf921_source.resolve_binding") as resolve:
        r = client.post("/context/assess", json=ASSESS_BODY, headers=_headers(classes=classes))
    assert r.status_code == 403 and r.json() == CLASS_REFUSED
    resolve.assert_not_called()
    _nothing_called(no_upstream)


def _binding(*devices, gaps=()):
    sources = {f"urn:m{i}": (device, "e0") for i, device in enumerate(devices)}
    return IntentBinding(intent_id="I1", sources=sources, bounds={}, gaps=gaps)


@pytest.mark.parametrize("extra", [{}, {"as_of": "10m"}, {"known_at": "1h"}, {"mode": "within"}])
def test_assess_refuses_an_intent_placed_on_a_device_outside_the_entities(extra, no_upstream):
    placed = _binding("Router-A", "Router-B")
    with patch("intent_management.tmf921_source.resolve_binding", return_value=placed):
        r = client.post("/context/assess", json={**ASSESS_BODY, **extra},
                        headers=_headers(entities=["Router-A"]))
    assert r.status_code == 403 and r.json() == ENTITY_REFUSED
    _nothing_called(no_upstream)  # no telemetry read, no TMF921 call, no push


def test_assess_proceeds_when_every_placed_device_is_covered(no_upstream):
    # A binding with a gap ends the assess before any upstream call, so a covered intent is answered.
    placed = _binding("Router-A", "Router-B", gaps=(("urn:m0", "no placement"),))
    with patch("intent_management.tmf921_source.resolve_binding", return_value=placed):
        r = client.post("/context/assess", json=ASSESS_BODY,
                        headers=_headers(entities=["Router-A", "Router-B"]))
    assert r.status_code == 200 and r.json()["verdict"] == "inconclusive"
    _nothing_called(no_upstream)


def test_assess_with_the_wildcard_covers_every_placed_device(no_upstream):
    placed = _binding("Router-A", "Router-B", gaps=(("urn:m0", "no placement"),))
    with patch("intent_management.tmf921_source.resolve_binding", return_value=placed):
        r = client.post("/context/assess", json=ASSESS_BODY, headers=_headers())
    assert r.status_code == 200


def test_the_refusal_names_no_device_or_intent(no_upstream):
    placed = _binding("Secret-Router-9")
    with patch("intent_management.tmf921_source.resolve_binding", return_value=placed):
        r = client.post("/context/assess", json=ASSESS_BODY, headers=_headers(entities=["Router-A"]))
    assert "Secret-Router-9" not in r.text and "I1" not in r.text


def test_the_in_process_assess_applies_no_authority():
    # The agent and the MCP server present no token and pass no `allowed`.
    placed = _binding("Router-B", gaps=(("urn:m0", "no placement"),))
    with patch("intent_management.tmf921_source.resolve_binding", return_value=placed):
        out = main.assess("I1", "5m")
    assert out["verdict"] == "inconclusive"
