"""Verify-only bearer-token authentication for the REST API (ACC-01).

The broker holds a public key only. Tokens are minted elsewhere (scripts/mint_token.py) from a
private key kept outside the repository and the image. Every request under /context/ must carry
a valid RS256 or ES256 token; an unauthenticated request is refused before routing, body parsing
or any upstream call. Responses and logs carry fixed strings only: never token text, claim
values or library messages.
"""
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from broker_engine.faults import log_fault

log = logging.getLogger(__name__)

ALGORITHMS = ("RS256", "ES256")
REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub")
MAX_TOKEN_LIFETIME_S = 3600
LEEWAY_S = 10
DEFAULT_AUDIENCE = "context-broker"
SCOPE_READ = "context:read"
SCOPE_ASSESS = "context:assess"
_KNOWN_SCOPES = (SCOPE_READ, SCOPE_ASSESS)

ENV_KEY_FILE = "BROKER_JWT_PUBLIC_KEY_FILE"
ENV_KEY = "BROKER_JWT_PUBLIC_KEY"
ENV_ISSUER = "BROKER_JWT_ISSUER"
ENV_AUDIENCE = "BROKER_JWT_AUDIENCE"

_PROTECTED_PREFIX = "/context"
_BEARER = "bearer "


class AuthError(Exception):
    """A refused request. The message is a fixed reason, never token or library text."""


@dataclass(frozen=True)
class AuthConfig:
    public_key: bytes
    issuer: str
    audience: str


# (path, st_mtime_ns, st_size) -> key bytes; a rewritten key file changes the stat and is re-read.
_key_cache: dict[tuple[str, int, int], bytes] = {}


def _read_key_file(path: str) -> bytes | None:
    try:
        st = os.stat(path)
        stamp = (path, st.st_mtime_ns, st.st_size)
        if stamp in _key_cache:
            return _key_cache[stamp]
        if not Path(path).is_file():
            return None
        data = Path(path).read_bytes()
    except (OSError, ValueError):
        return None
    _key_cache.clear()
    _key_cache[stamp] = data
    return data


def load_auth_config() -> AuthConfig | None:
    """The verification settings from the environment, or None when not configured.

    Read on every request, never at import, so key rotation and tests work. A missing, empty or
    unreadable key, or a missing issuer, gives None and every /context/ call is then 401. Never raises.
    """
    try:
        path = os.environ.get(ENV_KEY_FILE)
        if path:
            key = _read_key_file(path)
        else:
            pem = os.environ.get(ENV_KEY)
            key = pem.replace("\\n", "\n").encode() if pem else None
        issuer = os.environ.get(ENV_ISSUER)
        audience = os.environ.get(ENV_AUDIENCE) or DEFAULT_AUDIENCE
    except (OSError, ValueError, UnicodeError):
        return None
    if not key or not key.strip() or not issuer:
        return None
    return AuthConfig(public_key=key, issuer=issuer, audience=audience)


def verify_token(token: str, cfg: AuthConfig) -> dict:
    """Claims of a token signed by the configured key, or AuthError with a fixed reason."""
    try:
        claims = jwt.decode(
            token, cfg.public_key, algorithms=list(ALGORITHMS),
            audience=cfg.audience, issuer=cfg.issuer,
            options={"require": list(REQUIRED_CLAIMS)}, leeway=LEEWAY_S,
        )
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        log_fault(log, exc, "token refused")
        raise AuthError("invalid token") from None
    exp, iat = claims.get("exp"), claims.get("iat")
    if (isinstance(exp, bool) or isinstance(iat, bool)
            or not isinstance(exp, (int, float)) or not isinstance(iat, (int, float))
            or exp - iat > MAX_TOKEN_LIFETIME_S):
        raise AuthError("token lifetime too long")
    return claims


def scopes_of(claims: dict) -> frozenset[str]:
    """The known scopes in the space-delimited "scope" claim; anything else gives an empty set."""
    raw = claims.get("scope")
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(s for s in raw.split() if s in _KNOWN_SCOPES)


def authenticate(authorization_header: str | None) -> frozenset[str]:
    """The scopes of the bearer token in the header, or AuthError."""
    cfg = load_auth_config()
    if cfg is None:
        log.warning("authentication refused: not configured")
        raise AuthError("not configured")
    header = authorization_header or ""
    token = header[len(_BEARER):].strip() if header[:len(_BEARER)].lower() == _BEARER else ""
    if not token:
        log.warning("authentication refused: missing bearer token")
        raise AuthError("missing bearer token")
    return scopes_of(verify_token(token, cfg))


def _is_protected(path: str) -> bool:
    return path == _PROTECTED_PREFIX or path.startswith(_PROTECTED_PREFIX + "/")


_DENIED_BODY = {"detail": "authentication required"}


def _denied() -> JSONResponse:
    return JSONResponse(status_code=401, content=dict(_DENIED_BODY),
                        headers={"WWW-Authenticate": "Bearer"})


async def require_token(request: Request, call_next):
    """Default-deny HTTP middleware for /context/*; runs before routing and body parsing."""
    if _is_protected(request.url.path):
        try:
            request.state.scopes = authenticate(request.headers.get("authorization"))
        except AuthError:
            return _denied()
    return await call_next(request)


def need(scope: str):
    """A route dependency that answers 403 when the token lacks `scope`."""
    def check(request: Request) -> None:
        scopes = getattr(request.state, "scopes", None)
        if scopes is None:
            raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": "Bearer"})
        if scope not in scopes:
            raise HTTPException(403, "insufficient scope")

    return Depends(check)
