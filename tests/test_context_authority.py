"""Authority claims in the signed token and the Caller they produce (CTX-01, CTX-02).

The tracer mints a token with the real scripts/mint_token.py, verifies it with the real middleware
and reads the Caller inside a route, with only the environment set by the test.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from broker_engine import auth
from data_contracts.context_contract import CONTEXT_CLASSES, PURPOSES, Authority

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "mint_token.py"
ISSUER = "issuer-x"
AUTHORITY_DENIED = {"detail": "authority claims required"}
SCOPE_DENIED = {"detail": "insufficient scope"}

_KEY = ec.generate_private_key(ec.SECP256R1())


def _write_key(tmp_path) -> Path:
    path = tmp_path / "private.pem"
    path.write_bytes(_KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    path.chmod(0o600)
    return path


def _public_pem() -> str:
    return _KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _mint(tmp_path, *extra, sub="alice", scopes=("context:read",)) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BROKER_JWT_")}
    args = ["--key", str(_write_key(tmp_path)), "--sub", sub, "--issuer", ISSUER]
    for scope in scopes:
        args += ["--scope", scope]
    r = subprocess.run([sys.executable, str(SCRIPT), *args, *extra], capture_output=True, text=True,
                       env=env, cwd=str(REPO.parent))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def broker_env(monkeypatch):
    monkeypatch.delenv("BROKER_JWT_PUBLIC_KEY_FILE", raising=False)
    monkeypatch.setenv("BROKER_JWT_PUBLIC_KEY", _public_pem())
    monkeypatch.setenv("BROKER_JWT_ISSUER", ISSUER)
    monkeypatch.setenv("BROKER_JWT_AUDIENCE", "context-broker")


def _probe_app() -> tuple[FastAPI, list]:
    """A small app: the real middleware and a route that needs the scope, then the claims."""
    seen: list = []
    app = FastAPI()
    app.middleware("http")(auth.require_token)

    @app.post("/context/probe", dependencies=[auth.need(auth.SCOPE_READ), auth.need_authority_claims()])
    def probe(request: Request):
        seen.append(request.state.caller)
        return {"ok": True}

    return app, seen


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


AUTHORITY_FLAGS = ("--entity", "Router-A", "--purpose", "diagnosis", "--class", "topology")


# --- tracer: mint -> verify -> Caller -> dependency ---

def test_a_minted_token_reaches_the_route_as_a_caller_with_requester_and_authority(tmp_path, broker_env):
    token = _mint(tmp_path, *AUTHORITY_FLAGS)
    app, seen = _probe_app()
    r = TestClient(app).post("/context/probe", headers=_bearer(token))
    assert r.status_code == 200 and r.json() == {"ok": True}
    (caller,) = seen
    assert caller.requester == "alice"
    assert caller.scopes == frozenset({"context:read"})
    assert caller.authority.entities == frozenset({"Router-A"})
    assert caller.authority.purposes == frozenset({"diagnosis"})
    assert caller.authority.classes == frozenset({"topology"})


def test_the_minted_claim_has_the_documented_shape(tmp_path):
    token = _mint(tmp_path, "--entity", "Router-A", "--entity", "Router-A", "--entity", "Router-B",
                  "--purpose", "diagnosis", "--purpose", "assurance", "--purpose", "diagnosis",
                  "--class", "topology", "--class", "goals")
    cfg = auth.AuthConfig(public_key=_public_pem().encode(), issuer=ISSUER, audience="context-broker")
    claims = auth.verify_token(token, cfg)
    assert claims["authority"] == {"entities": ["Router-A", "Router-B"],
                                   "purposes": ["diagnosis", "assurance"],
                                   "classes": ["topology", "goals"]}


def test_a_token_with_the_scope_but_no_authority_claim_is_403_and_the_route_does_not_run(tmp_path, broker_env):
    token = _mint(tmp_path)
    app, seen = _probe_app()
    r = TestClient(app).post("/context/probe", headers=_bearer(token))
    assert r.status_code == 403 and r.json() == AUTHORITY_DENIED
    assert seen == []


def test_the_scope_is_checked_before_the_authority_claims(tmp_path, broker_env):
    token = _mint(tmp_path, *AUTHORITY_FLAGS, scopes=("context:assess",))
    app, seen = _probe_app()
    r = TestClient(app).post("/context/probe", headers=_bearer(token))
    assert r.status_code == 403 and r.json() == SCOPE_DENIED
    assert seen == []


def test_minting_without_authority_flags_leaves_the_token_as_before(tmp_path):
    token = _mint(tmp_path)
    cfg = auth.AuthConfig(public_key=_public_pem().encode(), issuer=ISSUER, audience="context-broker")
    assert "authority" not in auth.verify_token(token, cfg)


# --- authority_of ---

def _claim(entities=("Router-A",), purposes=("diagnosis",), classes=("topology",)) -> dict:
    return {"authority": {"entities": list(entities), "purposes": list(purposes), "classes": list(classes)}}


def test_authority_of_builds_an_authority_and_the_wildcard_covers_every_device():
    a = auth.authority_of(_claim(entities=("*",), purposes=("assurance",), classes=("goals", "observations")))
    assert isinstance(a, Authority)
    assert a.covers_entity("anything") is True
    assert a.classes == frozenset({"goals", "observations"})


def test_authority_of_an_empty_claim_set_is_none():
    assert auth.authority_of({}) is None


def test_the_closed_sets_are_the_documented_ones():
    assert PURPOSES == ("assurance", "diagnosis", "reporting")
    assert CONTEXT_CLASSES == ("topology", "goals", "observations", "events")
    assert auth.CLAIM_AUTHORITY == "authority"
    assert auth.AUTHORITY_KEYS == ("entities", "purposes", "classes")


# --- Task 2: malformed claims fail closed ---

import logging
import time
from unittest.mock import patch

import jwt

from broker_engine import main
from tests.auth_helpers import make_token

CANARY = "CANARY-ENTITY-77"
_GOOD = {"entities": ["Router-A"], "purposes": ["diagnosis"], "classes": ["topology"]}


def _with(**over) -> dict:
    return {"authority": {**_GOOD, **over}}


MALFORMED = {
    "no_claim": {},
    "claim_is_a_string": {"authority": "x"},
    "claim_is_a_list": {"authority": [_GOOD]},
    "claim_is_a_number": {"authority": 7},
    "claim_is_null": {"authority": None},
    "entities_missing": {"authority": {k: v for k, v in _GOOD.items() if k != "entities"}},
    "purposes_missing": {"authority": {k: v for k, v in _GOOD.items() if k != "purposes"}},
    "classes_missing": {"authority": {k: v for k, v in _GOOD.items() if k != "classes"}},
    "entities_is_a_string": _with(entities="Router-A"),
    "purposes_is_a_number": _with(purposes=5),
    "classes_is_null": _with(classes=None),
    "entities_empty": _with(entities=[]),
    "purposes_empty": _with(purposes=[]),
    "classes_empty": _with(classes=[]),
    "entity_glob_only": _with(entities=["R*"]),
    "entity_with_space": _with(entities=["Router A"]),
    "entity_empty_string": _with(entities=[""]),
    "entity_not_a_string": _with(entities=[7]),
    "entity_129_chars": _with(entities=["a" * 129]),
    "entity_newline": _with(entities=["Router-A\n"]),
    "purpose_unknown": _with(purposes=["admin"]),
    "class_unknown": _with(classes=["knowledge"]),
    "purpose_not_a_string": _with(purposes=[["diagnosis"]]),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_authority_of_never_grants_for_a_malformed_claim(case):
    assert auth.authority_of(MALFORMED[case]) is None


@pytest.mark.parametrize("value", [None, 0, 1.5, "x", [], [1, "a"], {"a": 1}, True])
def test_authority_of_is_total_for_any_json_value(value):
    assert auth.authority_of({"authority": value}) is None
    assert auth.authority_of({"authority": {"entities": value, "purposes": value, "classes": value}}) is None


def test_a_mixed_list_keeps_only_the_valid_part():
    a = auth.authority_of(_with(entities=["Router-A", "R*", 3, "bad name"],
                                purposes=["diagnosis", "admin"], classes=["topology", "knowledge"]))
    assert a.entities == {"Router-A"} and a.purposes == {"diagnosis"} and a.classes == {"topology"}


def test_extra_keys_inside_the_claim_are_ignored():
    a = auth.authority_of({"authority": {**_GOOD, "extra": ["x"]}})
    assert a is not None and a.entities == {"Router-A"}


def test_a_128_character_entity_is_kept():
    assert auth.authority_of(_with(entities=["a" * 128])).entities == {"a" * 128}


def test_covers_entity_is_exact_and_never_a_glob():
    star = auth.authority_of(_with(entities=["*"]))
    assert star.covers_entity("Router-A") and star.covers_entity("anything/at/all")
    named = auth.authority_of(_with(entities=["Router-A"]))
    assert named.covers_entity("Router-A")
    assert not named.covers_entity("Router-B") and not named.covers_entity("router-a")
    assert auth.authority_of(_with(entities=["R*"])) is None
    mixed = auth.authority_of(_with(entities=["Router-A", "R*"]))
    assert not mixed.covers_entity("Router-B") and not mixed.covers_entity("R*")


def _signed(**claims) -> str:
    # The probe app trusts the key generated at import by tests.auth_helpers; broker_env is not used here.
    return make_token(("context:read",), **claims)


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_a_malformed_claim_is_403_on_a_route_that_needs_it(case, caplog):
    app, seen = _probe_app()
    token = _signed(**MALFORMED[case], sub="canary-requester")
    with caplog.at_level(logging.DEBUG):
        r = TestClient(app).post("/context/probe", headers=_bearer(token))
    assert r.status_code == 403 and r.json() == AUTHORITY_DENIED
    assert seen == []
    assert "canary-requester" not in caplog.text and "canary-requester" not in r.text


def test_claim_values_never_reach_logs_or_responses(caplog):
    app, seen = _probe_app()
    token = _signed(authority={"entities": [CANARY + "*"], "purposes": ["diagnosis"], "classes": ["topology"]})
    with caplog.at_level(logging.DEBUG):
        r = TestClient(app).post("/context/probe", headers=_bearer(token))
    assert r.status_code == 403 and CANARY not in r.text and CANARY not in caplog.text
    ok = _signed(authority={"entities": [CANARY], "purposes": ["diagnosis"], "classes": ["topology"]})
    with caplog.at_level(logging.DEBUG):
        r = TestClient(app).post("/context/probe", headers=_bearer(ok))
    assert r.status_code == 200 and CANARY not in r.text and CANARY not in caplog.text


# --- requester refusals, on the real app, before any upstream call ---

SUBJECT_DENIED = {"detail": "authentication required"}
BAD_SUBJECTS = {
    "empty": "",
    "129_chars": "a" * 129,
    "newline": "ali\nce",
    "tab": "ali\tce",
    "nul": "ali\x00ce",
}
_METRIC = {"device": "r1", "interface": "e0", "metric_type": "latency_ms", "time_window": "5m"}
_app_client = TestClient(main.app)


@pytest.fixture
def no_upstream():
    with patch("requests.get") as get, patch("requests.post") as post, \
            patch("intent_management.tmf921_client.Tmf921Client.post_observation") as push:
        yield get, post, push


@pytest.mark.parametrize("case", sorted(BAD_SUBJECTS))
def test_an_unusable_requester_is_401_before_any_upstream_call(case, no_upstream, caplog):
    sub = BAD_SUBJECTS[case]
    token = _signed(sub=sub)
    with caplog.at_level(logging.DEBUG):
        r = _app_client.get("/context/metric", params=_METRIC, headers=_bearer(token))
    assert r.status_code == 401 and r.json() == SUBJECT_DENIED
    assert r.headers["www-authenticate"] == "Bearer"
    for mocked in no_upstream:
        mocked.assert_not_called()
    assert "authentication refused: invalid subject" in caplog.text
    assert token not in caplog.text
    if sub.strip():
        assert sub not in caplog.text


def test_a_canary_subject_is_never_logged_when_refused(no_upstream, caplog):
    sub = "CANARY-SUB-" + "z" * 120 + "\n"
    with caplog.at_level(logging.DEBUG):
        r = _app_client.get("/context/metric", params=_METRIC, headers=_bearer(_signed(sub=sub)))
    assert r.status_code == 401
    assert "CANARY-SUB" not in caplog.text and "CANARY-SUB" not in r.text


def test_a_subject_of_exactly_128_printable_characters_is_accepted():
    sub = "s" * 128
    with patch("broker_engine.main.operations.interface_neighbours", return_value={"ok": 1}):
        r = _app_client.get("/context/neighbours", params={"device": "r1", "interface": "e0"},
                            headers=_bearer(_signed(sub=sub)))
    assert r.status_code == 200


def test_authenticate_returns_the_subject_as_the_requester():
    caller = auth.authenticate("Bearer " + _signed(sub="alice", authority=_GOOD))
    assert caller.requester == "alice" and caller.authority.entities == {"Router-A"}


def test_old_routes_answer_as_before_for_a_token_without_authority(no_upstream):
    with patch("broker_engine.main.operations.interface_neighbours", return_value={"ok": 1}):
        r = _app_client.get("/context/neighbours", params={"device": "r1", "interface": "e0"},
                            headers=_bearer(_signed()))
    assert r.status_code == 200 and r.json() == {"ok": 1}
