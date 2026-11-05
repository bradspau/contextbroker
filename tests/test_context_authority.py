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
    assert CONTEXT_CLASSES == ("topology", "goals", "observations")
    assert auth.CLAIM_AUTHORITY == "authority"
    assert auth.AUTHORITY_KEYS == ("entities", "purposes", "classes")
