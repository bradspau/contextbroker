"""Test key pairs, JWT environment and token builder for the REST authentication tests.

Import this module only as ``tests.auth_helpers``. Importing it under a second name would
generate a second key pair, and tokens signed by one pair would not verify against the
public key the other pair put in the environment.

Keys are generated in memory at import time and never written to disk.
"""
import os
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

assert __name__ == "tests.auth_helpers", (
    f"import this module as tests.auth_helpers, not {__name__!r}: "
    "two imports would create two key pairs"
)

ISSUER = "contextbroker-tests"
AUDIENCE = "context-broker"


def _private_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _public_pem(key) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


_EC_KEY = ec.generate_private_key(ec.SECP256R1())
_OTHER_EC_KEY = ec.generate_private_key(ec.SECP256R1())
_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

EC_PRIVATE_PEM = _private_pem(_EC_KEY)
EC_PUBLIC_PEM = _public_pem(_EC_KEY)
OTHER_EC_PRIVATE_PEM = _private_pem(_OTHER_EC_KEY)
OTHER_EC_PUBLIC_PEM = _public_pem(_OTHER_EC_KEY)
RSA_PRIVATE_PEM = _private_pem(_RSA_KEY)
RSA_PUBLIC_PEM = _public_pem(_RSA_KEY)

os.environ["BROKER_JWT_PUBLIC_KEY"] = EC_PUBLIC_PEM.decode()
os.environ["BROKER_JWT_ISSUER"] = ISSUER
os.environ["BROKER_JWT_AUDIENCE"] = AUDIENCE
os.environ.pop("BROKER_JWT_PUBLIC_KEY_FILE", None)

_SIGNING = {
    "ec": (EC_PRIVATE_PEM, "ES256"),
    "other_ec": (OTHER_EC_PRIVATE_PEM, "ES256"),
    "rsa": (RSA_PRIVATE_PEM, "RS256"),
}


def make_token(scopes=("context:read", "context:assess"), *, key="ec",
               lifetime=3000, **claims) -> str:
    """Sign a token. A claim override of None removes that claim."""
    now = int(time.time())
    body = {
        "sub": "test-caller",
        "iss": os.environ.get("BROKER_JWT_ISSUER", ISSUER),
        "aud": os.environ.get("BROKER_JWT_AUDIENCE", AUDIENCE),
        "iat": now,
        "exp": now + lifetime,
    }
    if scopes is not None:
        body["scope"] = " ".join(scopes)
    body.update(claims)
    body = {k: v for k, v in body.items() if v is not None}
    pem, alg = _SIGNING[key]
    return jwt.encode(body, pem, algorithm=alg)


def auth_headers(*scopes: str) -> dict:
    return {"Authorization": "Bearer " + make_token(scopes or ("context:read", "context:assess"))}
