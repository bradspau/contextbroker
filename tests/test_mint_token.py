"""scripts/mint_token.py: local token minting from a private key kept outside the repository."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from broker_engine import auth

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "mint_token.py"


def _private_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _public_pem(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)


@pytest.fixture(scope="module")
def keys():
    return {
        "ec": ec.generate_private_key(ec.SECP256R1()),
        "rsa": rsa.generate_private_key(public_exponent=65537, key_size=2048),
        "ec384": ec.generate_private_key(ec.SECP384R1()),
        "rsa1024": rsa.generate_private_key(public_exponent=65537, key_size=1024),
    }


def _write_key(tmp_path, key, mode=0o600, name="private.pem") -> Path:
    path = tmp_path / name
    path.write_bytes(_private_pem(key))
    path.chmod(mode)
    return path


def _run(*args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BROKER_JWT_")}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env=env, cwd=str(REPO.parent))


def _cfg(key, issuer="issuer-x", audience="context-broker"):
    return auth.AuthConfig(public_key=_public_pem(key), issuer=issuer, audience=audience)


def _ok_args(path, *extra):
    return ["--key", str(path), "--sub", "alice", "--scope", "context:read", "--issuer", "issuer-x", *extra]


def test_minted_es256_token_passes_the_broker_verifier(tmp_path, keys):
    path = _write_key(tmp_path, keys["ec"])
    r = _run(*_ok_args(path))
    assert r.returncode == 0 and r.stderr == ""
    token = r.stdout.strip()
    assert token.count(".") == 2 and "\n" not in token
    claims = auth.verify_token(token, _cfg(keys["ec"]))
    assert claims["sub"] == "alice" and claims["scope"] == "context:read"
    assert claims["iss"] == "issuer-x" and claims["aud"] == "context-broker"
    assert claims["exp"] - claims["iat"] == 900
    assert jwt.get_unverified_header(token)["alg"] == "ES256"
    assert auth.scopes_of(claims) == {"context:read"}


def test_rsa_key_signs_rs256(tmp_path, keys):
    path = _write_key(tmp_path, keys["rsa"])
    r = _run(*_ok_args(path))
    assert r.returncode == 0
    token = r.stdout.strip()
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    auth.verify_token(token, _cfg(keys["rsa"]))


def test_scopes_ttl_and_audience_are_honoured(tmp_path, keys):
    path = _write_key(tmp_path, keys["ec"])
    r = _run("--key", str(path), "--sub", "bob", "--scope", "context:read", "--scope", "context:assess",
             "--ttl", "120", "--issuer", "issuer-x", "--audience", "other-aud")
    assert r.returncode == 0
    claims = auth.verify_token(r.stdout.strip(), _cfg(keys["ec"], audience="other-aud"))
    assert auth.scopes_of(claims) == {"context:read", "context:assess"}
    assert claims["exp"] - claims["iat"] == 120


def test_issuer_and_audience_default_from_the_environment(tmp_path, keys):
    path = _write_key(tmp_path, keys["ec"])
    r = _run("--key", str(path), "--sub", "alice", "--scope", "context:assess",
             env_extra={"BROKER_JWT_ISSUER": "env-issuer", "BROKER_JWT_AUDIENCE": "env-aud"})
    assert r.returncode == 0
    claims = auth.verify_token(r.stdout.strip(), _cfg(keys["ec"], issuer="env-issuer", audience="env-aud"))
    assert claims["iss"] == "env-issuer" and claims["aud"] == "env-aud"


def test_a_ttl_of_exactly_the_cap_is_allowed(tmp_path, keys):
    path = _write_key(tmp_path, keys["ec"])
    r = _run(*_ok_args(path, "--ttl", "3600"))
    assert r.returncode == 0
    auth.verify_token(r.stdout.strip(), _cfg(keys["ec"]))


def _assert_refused(r, message=None, secret=None):
    assert r.returncode == 2
    assert r.stdout == ""
    assert len(r.stderr.strip().splitlines()) == 1
    if message:
        assert message in r.stderr
    if secret:
        assert secret not in r.stderr
    assert "BEGIN" not in r.stderr and "eyJ" not in r.stderr


def test_key_inside_the_repository_is_refused_before_reading():
    r = _run(*_ok_args(REPO / "README.md"))
    _assert_refused(r, "the private key must be kept outside the repository")


def test_symlink_into_the_repository_is_refused(tmp_path):
    link = tmp_path / "link.pem"
    link.symlink_to(REPO / "README.md")
    _assert_refused(_run(*_ok_args(link)), "the private key must be kept outside the repository")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_key_readable_by_group_or_others_is_refused(tmp_path, keys, mode):
    path = _write_key(tmp_path, keys["ec"], mode=mode)
    _assert_refused(_run(*_ok_args(path)), "the private key file must not be readable by group or others")


def test_ttl_above_the_cap_is_refused(tmp_path, keys):
    path = _write_key(tmp_path, keys["ec"])
    _assert_refused(_run(*_ok_args(path, "--ttl", "3601")), "ttl")


@pytest.mark.parametrize("ttl", ["0", "-5"])
def test_non_positive_ttl_is_refused(tmp_path, keys, ttl):
    path = _write_key(tmp_path, keys["ec"])
    _assert_refused(_run(*_ok_args(path, "--ttl", ttl)), "ttl")


def test_missing_issuer_is_refused(tmp_path, keys):
    path = _write_key(tmp_path, keys["ec"])
    r = _run("--key", str(path), "--sub", "alice", "--scope", "context:read")
    _assert_refused(r, "issuer")


def test_missing_scope_is_refused(tmp_path, keys):
    path = _write_key(tmp_path, keys["ec"])
    r = _run("--key", str(path), "--sub", "alice", "--issuer", "issuer-x")
    _assert_refused(r, "scope")


@pytest.mark.parametrize("kind", ["ec384", "rsa1024"])
def test_unsupported_key_type_is_refused(tmp_path, keys, kind):
    path = _write_key(tmp_path, keys[kind])
    _assert_refused(_run(*_ok_args(path)), "the key must be an EC P-256 or an RSA key of at least 2048 bits")


def test_unreadable_pem_is_refused_without_echoing_it(tmp_path):
    path = tmp_path / "junk.pem"
    path.write_text("not a key SECRET-CANARY")
    path.chmod(0o600)
    _assert_refused(_run(*_ok_args(path)), secret="SECRET-CANARY")


def test_missing_key_file_is_refused(tmp_path):
    _assert_refused(_run(*_ok_args(tmp_path / "absent.pem")))


def test_an_unknown_scope_is_rejected_by_the_command_line(tmp_path, keys):
    path = _write_key(tmp_path, keys["ec"])
    r = _run("--key", str(path), "--sub", "alice", "--scope", "admin", "--issuer", "issuer-x")
    assert r.returncode == 2 and r.stdout == ""


def test_the_script_cap_equals_the_broker_cap():
    spec = importlib.util.spec_from_file_location("mint_token_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.MAX_TTL_S == auth.MAX_TOKEN_LIFETIME_S


def test_the_script_imports_no_broker_module():
    import ast
    imported = set()
    for node in ast.walk(ast.parse(SCRIPT.read_text())):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not imported & {"broker_engine", "intent_management", "data_contracts", "ingestion", "outcome_assurance"}


def _load_script():
    spec = importlib.util.spec_from_file_location("mint_token_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_authority_claim_round_trips_through_the_broker_parser(tmp_path, keys):
    path = _write_key(tmp_path, keys["ec"])
    r = _run(*_ok_args(path, "--entity", "Router-A", "--purpose", "diagnosis", "--class", "topology"))
    assert r.returncode == 0 and r.stderr == ""
    claims = auth.verify_token(r.stdout.strip(), _cfg(keys["ec"]))
    assert claims["authority"] == {"entities": ["Router-A"], "purposes": ["diagnosis"],
                                   "classes": ["topology"]}
    authority = auth.authority_of(claims)
    assert authority.entities == {"Router-A"}
    assert authority.purposes == {"diagnosis"} and authority.classes == {"topology"}


def test_the_script_constants_equal_the_broker_constants():
    from data_contracts import context_contract
    module = _load_script()
    assert module.PURPOSES == context_contract.PURPOSES
    assert module.CONTEXT_CLASSES == context_contract.CONTEXT_CLASSES
    assert module.CLAIM_AUTHORITY == auth.CLAIM_AUTHORITY
    assert (module.KEY_ENTITIES, module.KEY_PURPOSES, module.KEY_CLASSES) == auth.AUTHORITY_KEYS
