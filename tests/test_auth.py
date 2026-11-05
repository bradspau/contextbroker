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


# --- shared helpers for the matrix ---

import base64
import hashlib
import hmac
import json
import logging
import time

from broker_engine import auth
from data_contracts.envelope import build_observed
from tests.auth_helpers import EC_PUBLIC_PEM, OTHER_EC_PUBLIC_PEM, RSA_PUBLIC_PEM

ASSESS_BODY = {"intent_id": "I1", "time_window": "5m"}
REQUEST_BODY = {"purpose": "diagnosis", "scope": {"device": "r1"}, "classes": ["topology"],
                "max_depth": 1, "relationship_types": ["has_interface"]}
# Each POST route has its own body: one body for every path would be refused by the other route's model.
POST_BODIES = {"/context/assess": ASSESS_BODY, "/context/request": REQUEST_BODY}
AUTHORITY = {"entities": ["*"], "purposes": ["diagnosis"], "classes": ["topology"]}
GET_ROUTES = {
    "/context/metric": METRIC,
    "/context/metric-history": {**METRIC, "metric_type": "input_drops"},
    "/context/interfaces-nonzero": {"device": "r1", "metric_type": "input_drops", "time_window": "1h"},
    "/context/neighbours": NEIGHBOURS,
}


def _context_routes():
    out = []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/context/"):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                out.append((method, path))
    return out


def _call(method, path, headers=None, c=client):
    if method == "POST":
        return c.post(path, json=POST_BODIES[path], headers=headers)
    return c.get(path, params=GET_ROUTES[path], headers=headers)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _b64(data) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode() if not isinstance(data, bytes) else data
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _claims(**over):
    now = int(time.time())
    body = {"sub": "test-caller", "iss": "contextbroker-tests", "aud": "context-broker",
            "iat": now, "exp": now + 3000, "scope": "context:read context:assess"}
    body.update(over)
    return body


def _hs256_with_public_key_as_secret():
    head, body = _b64({"alg": "HS256", "typ": "JWT"}), _b64(_claims())
    sig = hmac.new(EC_PUBLIC_PEM, f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


def _alg_none():
    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(_claims())}."


def _minimal_response():
    """The smallest valid ContextResponse: the route validates what its handler returns."""
    from data_contracts.context_contract import (
        ClassEligibility, ContextResponse, Eligibility, Scope, Situation)
    stamp = "2026-11-06T12:00:00+00:00"
    return ContextResponse(
        situation=Situation(
            id="sit-0", requester="test-caller", purpose="diagnosis", scope=Scope(device="r1"),
            intent_id=None, classes=["topology"], max_depth=1, relationship_types=["has_interface"],
            valid_at=stamp, known_at=None, retrieved_at=stamp, historical=False),
        eligibility=Eligibility(
            result="gap", classes={"topology": ClassEligibility(result="gap", reasons=["x"])},
            reasons=["x"]),
        expires_at=stamp, expiry_basis="no_evidence", fluent_values=[], envelopes=[], graph=None,
        verdict=None)


def _patched_handlers():
    """Stub every handler so a request that passes authentication returns 200 with no upstream."""
    env = build_observed("r1:e0", 1.0, "vm", None, None)
    return (
        patch("broker_engine.main.join_observed_metric", return_value=env),
        patch("broker_engine.main.operations.metric_history", return_value={"ok": 1}),
        patch("broker_engine.main.operations.interfaces_with_nonzero", return_value={"ok": 1}),
        patch("broker_engine.main.operations.interface_neighbours", return_value={"ok": 1}),
        patch("broker_engine.main.assess", return_value={"verdict": "success"}),
        patch("broker_engine.main.context_request.handle", return_value=_minimal_response()),
    )


@pytest.fixture
def handlers():
    patches = _patched_handlers()
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


@pytest.fixture
def no_upstream():
    with patch("requests.get") as get, patch("requests.post") as post, \
            patch("intent_management.tmf921_client.Tmf921Client.post_observation") as push:
        yield get, post, push


def _assert_denied(r):
    assert r.status_code == 401
    assert r.json() == DENIED
    assert r.headers["www-authenticate"] == "Bearer"


# --- route inventory: no route is reachable without a token ---

def test_route_inventory_covers_the_six_context_routes():
    assert sorted(_context_routes()) == sorted(
        [("GET", p) for p in GET_ROUTES] + [("POST", p) for p in POST_BODIES])


@pytest.mark.parametrize("method,path", _context_routes())
def test_every_context_route_without_token_is_401_and_reaches_no_upstream(method, path, no_upstream):
    _assert_denied(_call(method, path))
    for mocked in no_upstream:
        mocked.assert_not_called()


def test_malformed_json_without_token_is_401_not_422(no_upstream):
    r = client.post("/context/assess", content="{not json", headers={"Content-Type": "application/json"})
    _assert_denied(r)
    for mocked in no_upstream:
        mocked.assert_not_called()


def test_malformed_json_with_assess_token_is_422(handlers):
    r = client.post("/context/assess", content="{not json",
                    headers={**auth_headers("context:assess"), "Content-Type": "application/json"})
    assert r.status_code == 422


def test_unknown_context_path_without_token_is_401(no_upstream):
    _assert_denied(client.get("/context/nope"))


def test_unknown_context_path_with_token_is_404():
    assert client.get("/context/nope", headers=auth_headers("context:read")).status_code == 404


def test_bare_context_prefix_without_token_is_401():
    _assert_denied(client.get("/context"))


# --- token matrix: every defect is the same fixed 401 ---

_NOW = lambda: int(time.time())  # noqa: E731

BAD_HEADERS = {
    "no_header": lambda: {},
    "empty_bearer": lambda: {"Authorization": "Bearer "},
    "basic_scheme": lambda: {"Authorization": "Basic dXNlcjpwYXNz"},
    "garbage_token": lambda: _bearer("not.a.token"),
    "expired_beyond_leeway": lambda: _bearer(make_token(iat=_NOW() - 200, exp=_NOW() - 100)),
    "no_exp": lambda: _bearer(make_token(exp=None)),
    "no_iat": lambda: _bearer(make_token(iat=None)),
    "no_sub": lambda: _bearer(make_token(sub=None)),
    "no_iss": lambda: _bearer(make_token(iss=None)),
    "no_aud": lambda: _bearer(make_token(aud=None)),
    "wrong_aud": lambda: _bearer(make_token(aud="another-service")),
    "wrong_iss": lambda: _bearer(make_token(iss="someone-else")),
    "non_string_sub": lambda: _bearer(make_token(sub=123)),
    "iat_in_future": lambda: _bearer(make_token(iat=_NOW() + 120, exp=_NOW() + 1000)),
    "lifetime_3601s": lambda: _bearer(make_token(lifetime=3601)),
    "hs256_public_key_as_secret": lambda: _bearer(_hs256_with_public_key_as_secret()),
    "alg_none": lambda: _bearer(_alg_none()),
    "rs256_while_broker_holds_ec_key": lambda: _bearer(make_token(key="rsa")),
    "es256_from_a_different_key": lambda: _bearer(make_token(key="other_ec")),
}


@pytest.mark.parametrize("case", sorted(BAD_HEADERS))
def test_bad_credential_is_401(case, no_upstream):
    _assert_denied(client.get("/context/metric", params=METRIC, headers=BAD_HEADERS[case]()))
    for mocked in no_upstream:
        mocked.assert_not_called()


def test_lifetime_of_exactly_3600s_is_accepted(handlers):
    r = client.get("/context/metric", params=METRIC, headers=_bearer(make_token(lifetime=3600)))
    assert r.status_code == 200


def test_bearer_scheme_is_case_insensitive(handlers):
    token = make_token()
    assert client.get("/context/metric", params=METRIC,
                      headers={"Authorization": f"bearer {token}"}).status_code == 200


def test_rs256_token_is_accepted_when_the_broker_holds_the_rsa_key(monkeypatch, handlers):
    monkeypatch.setenv("BROKER_JWT_PUBLIC_KEY", RSA_PUBLIC_PEM.decode())
    assert client.get("/context/metric", params=METRIC,
                      headers=_bearer(make_token(key="rsa"))).status_code == 200
    _assert_denied(client.get("/context/metric", params=METRIC, headers=_bearer(make_token(key="ec"))))


def test_key_pem_with_literal_backslash_n_is_accepted(monkeypatch, handlers):
    monkeypatch.setenv("BROKER_JWT_PUBLIC_KEY", EC_PUBLIC_PEM.decode().strip().replace("\n", "\\n"))
    assert client.get("/context/metric", params=METRIC, headers=auth_headers("context:read")).status_code == 200


# --- no key configured (D-07) ---

@pytest.mark.parametrize("missing", [
    ("BROKER_JWT_PUBLIC_KEY",), ("BROKER_JWT_ISSUER",), ("BROKER_JWT_PUBLIC_KEY", "BROKER_JWT_ISSUER"),
], ids=["no_key", "no_issuer", "neither"])
@pytest.mark.parametrize("method,path", _context_routes())
def test_unconfigured_api_refuses_every_context_route(missing, method, path, monkeypatch, no_upstream):
    headers = auth_headers("context:read", "context:assess")
    for name in missing:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("BROKER_JWT_PUBLIC_KEY_FILE", raising=False)
    _assert_denied(_call(method, path, headers=headers))


def test_unconfigured_api_still_serves_openapi(monkeypatch):
    for name in ("BROKER_JWT_PUBLIC_KEY", "BROKER_JWT_PUBLIC_KEY_FILE", "BROKER_JWT_ISSUER"):
        monkeypatch.delenv(name, raising=False)
    assert auth.load_auth_config() is None
    assert client.get("/openapi.json").status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_documentation_routes_stay_public(path):
    assert client.get(path).status_code == 200


# --- key file and rotation (D-06) ---

def _use_key_file(monkeypatch, tmp_path, pem: bytes):
    path = tmp_path / "broker_jwt_public.pem"
    path.write_bytes(pem)
    monkeypatch.setenv("BROKER_JWT_PUBLIC_KEY_FILE", str(path))
    return path


def test_key_file_is_used(monkeypatch, tmp_path, handlers):
    _use_key_file(monkeypatch, tmp_path, EC_PUBLIC_PEM)
    assert client.get("/context/metric", params=METRIC, headers=auth_headers("context:read")).status_code == 200


def test_empty_key_file_means_no_key(monkeypatch, tmp_path, no_upstream):
    _use_key_file(monkeypatch, tmp_path, b"")
    assert auth.load_auth_config() is None
    _assert_denied(client.get("/context/metric", params=METRIC, headers=auth_headers("context:read")))


def test_directory_as_key_path_means_no_key(monkeypatch, tmp_path, no_upstream):
    monkeypatch.setenv("BROKER_JWT_PUBLIC_KEY_FILE", str(tmp_path))
    assert auth.load_auth_config() is None
    _assert_denied(client.get("/context/metric", params=METRIC, headers=auth_headers("context:read")))


def test_missing_key_file_means_no_key(monkeypatch, tmp_path, no_upstream):
    monkeypatch.setenv("BROKER_JWT_PUBLIC_KEY_FILE", str(tmp_path / "absent.pem"))
    assert auth.load_auth_config() is None
    _assert_denied(client.get("/context/metric", params=METRIC, headers=auth_headers("context:read")))


def test_garbage_key_file_is_401_not_a_crash(monkeypatch, tmp_path, no_upstream):
    _use_key_file(monkeypatch, tmp_path, b"-----BEGIN PUBLIC KEY-----\nnope\n-----END PUBLIC KEY-----\n")
    _assert_denied(client.get("/context/metric", params=METRIC, headers=auth_headers("context:read")))


def test_replacing_the_key_file_cuts_off_tokens_of_the_old_key(monkeypatch, tmp_path, handlers):
    old_token, new_token = make_token(key="ec"), make_token(key="other_ec")
    path = _use_key_file(monkeypatch, tmp_path, EC_PUBLIC_PEM)
    assert client.get("/context/metric", params=METRIC, headers=_bearer(old_token)).status_code == 200
    path.write_bytes(OTHER_EC_PUBLIC_PEM)  # rewritten at once, as an operator rotating the key would
    _assert_denied(client.get("/context/metric", params=METRIC, headers=_bearer(old_token)))
    assert client.get("/context/metric", params=METRIC, headers=_bearer(new_token)).status_code == 200


# --- scope matrix (D-04) ---

@pytest.mark.parametrize("path", sorted(GET_ROUTES))
def test_read_only_token_reads(path, handlers):
    assert _call("GET", path, auth_headers("context:read")).status_code == 200


@pytest.mark.parametrize("path", sorted(GET_ROUTES))
def test_assess_only_token_cannot_read(path, handlers):
    r = _call("GET", path, auth_headers("context:assess"))
    assert r.status_code == 403 and r.json() == SCOPE_DENIED


def test_assess_token_can_assess(handlers):
    assert _call("POST", "/context/assess", auth_headers("context:assess")).status_code == 200


def _with_authority(*scopes):
    return {"Authorization": "Bearer " + make_token(scopes, authority=AUTHORITY)}


def test_read_token_with_authority_reaches_the_context_request_handler(handlers):
    r = _call("POST", "/context/request", _with_authority("context:read"))
    assert r.status_code == 200
    assert r.json()["situation"]["requester"] == "test-caller"


def test_read_token_without_authority_is_403_with_no_upstream_call(handlers, no_upstream):
    r = _call("POST", "/context/request", auth_headers("context:read"))
    assert r.status_code == 403 and r.json() == {"detail": "authority claims required"}
    for mocked in no_upstream:
        mocked.assert_not_called()


def test_assess_only_token_with_authority_cannot_request_context(handlers, no_upstream):
    r = _call("POST", "/context/request", _with_authority("context:assess"))
    assert r.status_code == 403 and r.json() == SCOPE_DENIED
    for mocked in no_upstream:
        mocked.assert_not_called()


def test_read_only_token_cannot_assess_and_pushes_nothing(handlers, no_upstream):
    r = _call("POST", "/context/assess", auth_headers("context:read"))
    assert r.status_code == 403 and r.json() == SCOPE_DENIED
    for mocked in no_upstream:
        mocked.assert_not_called()


SCOPELESS = {
    "no_scope_claim": lambda: make_token(scopes=None),
    "unknown_scopes": lambda: make_token(scopes=("admin", "context:write")),
    "non_string_scope": lambda: make_token(scopes=None, scope=["context:read"]),
    "numeric_scope": lambda: make_token(scopes=None, scope=7),
}


@pytest.mark.parametrize("case", sorted(SCOPELESS))
@pytest.mark.parametrize("method,path", _context_routes())
def test_token_without_a_known_scope_is_403_everywhere(case, method, path, handlers):
    r = _call(method, path, _bearer(SCOPELESS[case]()))
    assert r.status_code == 403 and r.json() == SCOPE_DENIED


def test_scope_is_checked_before_the_query_is_validated(handlers):
    r = client.get("/context/metric", params={"device": "r1"}, headers=auth_headers("context:assess"))
    assert r.status_code == 403


def test_authenticated_validation_errors_are_unchanged(handlers):
    r = client.get("/context/metric", params={**METRIC, "extra": "x"}, headers=auth_headers("context:read"))
    assert r.status_code == 422


# --- logging and secrecy (T-01-29) ---

def test_expired_token_is_logged_by_class_only(caplog):
    token = make_token(sub="canary-subject-93", iat=_NOW() - 200, exp=_NOW() - 100)
    with caplog.at_level(logging.WARNING):
        r = client.get("/context/metric", params=METRIC, headers=_bearer(token))
    assert r.status_code == 401 and r.json() == DENIED
    assert "token refused: ExpiredSignatureError" in caplog.text
    assert token not in caplog.text and token not in r.text
    assert "canary-subject-93" not in caplog.text and "canary-subject-93" not in r.text


def test_missing_token_is_logged_with_a_fixed_reason(caplog):
    with caplog.at_level(logging.WARNING):
        client.get("/context/metric", params=METRIC)
    assert "authentication refused: missing bearer token" in caplog.text


def test_unconfigured_refusal_is_logged_with_a_fixed_reason(monkeypatch, caplog):
    monkeypatch.delenv("BROKER_JWT_ISSUER", raising=False)
    with caplog.at_level(logging.WARNING):
        client.get("/context/metric", params=METRIC, headers=auth_headers("context:read"))
    assert "authentication refused: not configured" in caplog.text


# --- pure units ---

def test_verify_token_returns_the_claims():
    cfg = auth.load_auth_config()
    claims = auth.verify_token(make_token(), cfg)
    assert claims["sub"] == "test-caller" and claims["aud"] == "context-broker"


def test_verify_token_lifetime_error_is_a_fixed_reason():
    with pytest.raises(auth.AuthError, match="^token lifetime too long$"):
        auth.verify_token(make_token(lifetime=3601), auth.load_auth_config())


def test_verify_token_invalid_error_carries_no_library_text():
    with pytest.raises(auth.AuthError) as info:
        auth.verify_token("garbage", auth.load_auth_config())
    assert str(info.value) == "invalid token" and info.value.__cause__ is None


@pytest.mark.parametrize("claim,expected", [
    ("context:read", {"context:read"}),
    ("context:read context:assess", {"context:read", "context:assess"}),
    ("  context:assess   admin ", {"context:assess"}),
    ("", set()),
    (None, set()),
    (["context:read"], set()),
    (5, set()),
])
def test_scopes_of(claim, expected):
    assert auth.scopes_of({"scope": claim}) == expected


def test_scopes_of_without_a_scope_claim():
    assert auth.scopes_of({}) == frozenset()


def test_the_audience_defaults_to_the_mcp_server_name(monkeypatch):
    monkeypatch.delenv("BROKER_JWT_AUDIENCE", raising=False)
    assert auth.load_auth_config().audience == "context-broker"


def test_the_issuer_has_no_default(monkeypatch):
    monkeypatch.delenv("BROKER_JWT_ISSUER", raising=False)
    assert auth.load_auth_config() is None
