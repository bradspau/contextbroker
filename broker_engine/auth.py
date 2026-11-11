"""Verify-only bearer-token authentication for the REST API (ACC-01).

The broker holds a public key only. Tokens are minted elsewhere (scripts/mint_token.py) from a
private key kept outside the repository and the image. Every request under /context/ must carry
a valid RS256 or ES256 token; an unauthenticated request is refused before routing, body parsing
or any upstream call. Responses and logs carry fixed strings only: never token text, claim
values or library messages.

The requester is the token's `sub` (never a request field). Authority, meaning which entities,
purposes and context classes a caller may ask about, rides in the signed `authority` claim, so the
broker stays verify-only with no deny list. A route that needs it declares need(SCOPE_...) and then
need_authority_claims(); a token with the scope but no usable authority claim is refused with a
fixed reason before the route body runs.
"""
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from broker_engine.faults import log_fault
from broker_engine.validation import SAFE_TOKEN
from data_contracts.context_contract import CONTEXT_CLASSES, ENTITY_WILDCARD, PURPOSES, Authority

log = logging.getLogger(__name__)

ALGORITHMS = ("RS256", "ES256")
REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub")
MAX_TOKEN_LIFETIME_S = 3600
LEEWAY_S = 10
DEFAULT_AUDIENCE = "context-broker"
SCOPE_READ = "context:read"
SCOPE_ASSESS = "context:assess"
_KNOWN_SCOPES = (SCOPE_READ, SCOPE_ASSESS)
CLAIM_AUTHORITY = "authority"
AUTHORITY_KEYS = ("entities", "purposes", "classes")  # scripts/mint_token.py keeps a copy (tests pin it)
MAX_REQUESTER_LENGTH = 128
MAX_ENTITY_LENGTH = 128  # scripts/mint_token.py keeps a copy (tests pin it)

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


@dataclass(frozen=True)
class Caller:
    """Who is asking and under what authority; authority is None when the claims are absent or unusable."""
    requester: str
    scopes: frozenset[str]
    authority: Authority | None


# (path, st_mtime_ns, st_size, st_ino, st_ctime_ns) -> key bytes; a rewritten or replaced key file
# changes the stat and is re-read.
_key_cache: dict[tuple[str, int, int, int, int], bytes] = {}


def _read_key_file(path: str) -> bytes | None:
    try:
        st = os.stat(path)
        stamp = (path, st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)
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


def _usable_entity(item) -> bool:
    return isinstance(item, str) and (
        item == ENTITY_WILDCARD
        or (len(item) <= MAX_ENTITY_LENGTH and SAFE_TOKEN.match(item) is not None))


def authority_of(claims: dict) -> Authority | None:
    """The authority in the "authority" claim, or None. Pure and total: it never raises.

    The claim must be a dict whose three keys each hold a list. Entities keep only "*" (exact) and
    safe device names, so a glob is dropped; purposes and classes keep only the closed sets. If any
    of the three is empty after that, the result is None: a partial authority grants nothing.
    """
    raw = claims.get(CLAIM_AUTHORITY) if isinstance(claims, dict) else None
    if not isinstance(raw, dict):
        return None
    lists = [raw.get(key) for key in AUTHORITY_KEYS]
    if not all(isinstance(item, list) for item in lists):
        return None
    entities = frozenset(i for i in lists[0] if _usable_entity(i))
    purposes = frozenset(i for i in lists[1] if isinstance(i, str) and i in PURPOSES)
    classes = frozenset(i for i in lists[2] if isinstance(i, str) and i in CONTEXT_CLASSES)
    if not (entities and purposes and classes):
        return None
    return Authority(entities=entities, purposes=purposes, classes=classes)


def authenticate(authorization_header: str | None) -> Caller:
    """The Caller for the bearer token in the header, or AuthError."""
    cfg = load_auth_config()
    if cfg is None:
        log.warning("authentication refused: not configured")
        raise AuthError("not configured")
    header = authorization_header or ""
    token = header[len(_BEARER):].strip() if header[:len(_BEARER)].lower() == _BEARER else ""
    if not token:
        log.warning("authentication refused: missing bearer token")
        raise AuthError("missing bearer token")
    claims = verify_token(token, cfg)
    requester = claims.get("sub")
    if not (isinstance(requester, str) and 0 < len(requester) <= MAX_REQUESTER_LENGTH
            and requester.isprintable()):
        # An empty sub would make anonymous callers share one requester and one situation identity.
        log.warning("authentication refused: invalid subject")
        raise AuthError("invalid token")
    return Caller(requester=requester, scopes=scopes_of(claims), authority=authority_of(claims))


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
            caller = authenticate(request.headers.get("authorization"))
            request.state.scopes = caller.scopes
            request.state.caller = caller
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


def need_authority_claims():
    """A route dependency that answers 403 when the token carries no usable authority claims.

    Declare it after need(scope): the scope is checked first. The reason is fixed text, never a
    claim value.
    """
    def check(request: Request) -> None:
        caller = getattr(request.state, "caller", None)
        if caller is None:
            raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": "Bearer"})
        if caller.authority is None:
            raise HTTPException(403, "authority claims required")

    return Depends(check)
