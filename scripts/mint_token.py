#!/usr/bin/env python3
"""Mint a short-lived bearer token for the Context Broker REST API.

usage: python scripts/mint_token.py --key ~/keys/broker_jwt_private.pem --sub alice \\
           --scope context:read --issuer my-issuer [--ttl 900] [--audience context-broker] \\
           [--entity Router-A --purpose diagnosis --class topology]

The broker only verifies tokens; it never holds a private key. This operator tool signs one
with a private key that must live outside the repository and must not be readable by group or
others. EC P-256 keys sign ES256 and RSA keys of at least 2048 bits sign RS256. Only the token
is printed to stdout. A refusal prints one fixed line to stderr and exits 2.

Standalone on purpose: standard library, PyJWT and cryptography only, no broker modules.
"""
import argparse
import os
import re
import stat
import sys
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

MAX_TTL_S = 3600  # equals broker_engine.auth.MAX_TOKEN_LIFETIME_S (tests/test_mint_token.py)
DEFAULT_TTL_S = 900
DEFAULT_AUDIENCE = "context-broker"
SCOPES = ("context:read", "context:assess")
# Copies of data_contracts.context_contract.PURPOSES / CONTEXT_CLASSES and broker_engine.auth
# CLAIM_AUTHORITY / AUTHORITY_KEYS; tests/test_mint_token.py pins them equal.
PURPOSES = ("assurance", "diagnosis", "reporting")
CONTEXT_CLASSES = ("topology", "goals", "observations", "events")
CLAIM_AUTHORITY = "authority"
KEY_ENTITIES, KEY_PURPOSES, KEY_CLASSES = "entities", "purposes", "classes"
MAX_ENTITY_LENGTH = 128
# The same pattern as broker_engine.validation._SAFE_TOKEN (standalone copy).
_SAFE_ENTITY = re.compile(r"^[A-Za-z0-9_\-/]+\Z")
MIN_RSA_BITS = 2048
REPO = Path(__file__).resolve().parent.parent


class Refusal(Exception):
    """A refusal with a fixed one-line message; never carries key material."""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Mint a bearer token for the Context Broker REST API.")
    p.add_argument("--key", required=True, help="path to the private key PEM, outside the repository")
    p.add_argument("--sub", required=True, help="token subject (the requester)")
    p.add_argument("--scope", action="append", choices=SCOPES, help="repeatable; at least one")
    p.add_argument("--entity", action="append",
                   help="repeatable; a device name, or * for all (authority claim)")
    p.add_argument("--purpose", action="append", choices=PURPOSES,
                   help="repeatable; a purpose the caller may name (authority claim)")
    p.add_argument("--class", dest="classes", action="append", choices=CONTEXT_CLASSES,
                   help="repeatable; a context class the caller may ask for (authority claim)")
    p.add_argument("--ttl", type=int, default=DEFAULT_TTL_S, help=f"seconds, at most {MAX_TTL_S}")
    p.add_argument("--issuer", default=os.environ.get("BROKER_JWT_ISSUER"),
                   help="default: BROKER_JWT_ISSUER")
    p.add_argument("--audience", default=os.environ.get("BROKER_JWT_AUDIENCE") or DEFAULT_AUDIENCE,
                   help=f"default: BROKER_JWT_AUDIENCE, else {DEFAULT_AUDIENCE}")
    return p


def _load_key(key_arg: str):
    path = Path(key_arg).expanduser().resolve()
    if path == REPO or REPO in path.parents:
        raise Refusal("the private key must be kept outside the repository")
    try:
        mode = path.stat().st_mode
    except OSError:
        raise Refusal("the private key file could not be read") from None
    if not stat.S_ISREG(mode):
        raise Refusal("the private key file could not be read")
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise Refusal("the private key file must not be readable by group or others")
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        raise Refusal("the private key file could not be read as an unencrypted PEM key") from None
    if isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1):
        return key, "ES256"
    if isinstance(key, rsa.RSAPrivateKey) and key.key_size >= MIN_RSA_BITS:
        return key, "RS256"
    raise Refusal(f"the key must be an EC P-256 or an RSA key of at least {MIN_RSA_BITS} bits")


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not args.scope:
            raise Refusal("at least one --scope is required")
        given = [bool(args.entity), bool(args.purpose), bool(args.classes)]
        if any(given) and not all(given):
            raise Refusal("--entity, --purpose and --class go together: give all three or none")
        for entity in args.entity or []:
            if entity != "*" and not (len(entity) <= MAX_ENTITY_LENGTH and _SAFE_ENTITY.match(entity)):
                raise Refusal("an --entity must be * or a device name of letters, digits, _, - or /, "
                              f"at most {MAX_ENTITY_LENGTH} characters")
        if not 0 < args.ttl <= MAX_TTL_S:
            raise Refusal(f"--ttl must be between 1 and {MAX_TTL_S} seconds")
        if not args.issuer:
            raise Refusal("an issuer is required: pass --issuer or set BROKER_JWT_ISSUER")
        key, algorithm = _load_key(args.key)
    except Refusal as exc:
        print(str(exc), file=sys.stderr)
        return 2
    now = int(time.time())
    claims = {
        "sub": args.sub, "iss": args.issuer, "aud": args.audience,
        "iat": now, "exp": now + args.ttl, "scope": " ".join(dict.fromkeys(args.scope)),
    }
    if args.entity or args.purpose or args.classes:
        claims[CLAIM_AUTHORITY] = {
            KEY_ENTITIES: list(dict.fromkeys(args.entity or [])),
            KEY_PURPOSES: list(dict.fromkeys(args.purpose or [])),
            KEY_CLASSES: list(dict.fromkeys(args.classes or [])),
        }
    print(jwt.encode(claims, key, algorithm=algorithm))
    return 0


if __name__ == "__main__":
    sys.exit(main())
