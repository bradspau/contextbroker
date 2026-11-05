"""The logical context request and response contract, independent of its wire format (D-06).

This module is pure: no FastAPI, no I/O. It is the one tracked place for the closed sets a request is
expressed in (the purposes it can name, the context classes it can ask for, the relationship types a
traversal follows) and for the request model, the authority check, the situation identity, the time
and expiry rules and the response models. The HTTP layer only wires it.
scripts/mint_token.py is standalone and keeps a copy of PURPOSES and CONTEXT_CLASSES; a test pins the
copy equal.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from broker_engine.validation import freshness_seconds, window_seconds
from data_contracts.envelope import DEFAULT_MAX_AGE_S

PURPOSES = ("assurance", "diagnosis", "reporting")
CONTEXT_CLASSES = ("topology", "goals", "observations")
RELATIONSHIP_TYPES = ("has_interface", "connected_to")

# Traversal limits (D-23). The hard maximum depth is refused at validation, so it is never a cut
# reason. The node cap equals DEFAULT_MAX_ENTITIES in intent_management/tmf921_source.py.
MAX_DEPTH = 3
NODE_CAP = 50
# What can cut a traversal result (D-23, D-26).
CUT_REASONS = ("authority", "depth", "node_cap", "relationship_types")
# Eligibility outcomes (D-15).
RESULTS = ("eligible", "ineligible", "gap")

# The entity wildcard is this exact string. No glob matching of any kind (D-09).
ENTITY_WILDCARD = "*"

# Fixed refusal details for a request outside the caller's authority (D-09, D-10).
REFUSED_ENTITY = "outside authority scope: entity"
REFUSED_PURPOSE = "outside authority scope: purpose"
REFUSED_CLASS = "outside authority scope: class"

# Why an expiry is what it is (D-13, D-14, D-30).
EXPIRY_HISTORICAL = "historical_request"
EXPIRY_OBSERVATION = "oldest_observation"
EXPIRY_CONFIGURED = "configured_state"
EXPIRY_NO_EVIDENCE = "no_evidence"

# Bumped if the situation identity construction ever changes (D-11, D-27).
SITUATION_VERSION = 1

# A device, interface or intent token as the request grammar allows it. The device and interface
# pattern is validation._SAFE_TOKEN written with `$` (pydantic's pattern engine has no `\Z`; `$` is
# end-of-string there and refuses a trailing newline). The intent pattern is the TMF921 client's.
_TOKEN_PATTERN = r"^[A-Za-z0-9_\-/]+$"
_INTENT_PATTERN = r"^[A-Za-z0-9-]+$"
_OFFSET_PATTERN = r"^[0-9]+[smhd]$"
_MAX_TOKEN_LENGTH = 128
_MAX_INTENT_LENGTH = 64
_MAX_OFFSET_LENGTH = 12

_Purpose = Literal[PURPOSES]
_ContextClass = Literal[CONTEXT_CLASSES]
_RelationshipType = Literal[RELATIONSHIP_TYPES]


@dataclass(frozen=True)
class Authority:
    """What a caller may ask about: devices, purposes and context classes, from the signed token."""

    entities: frozenset[str]
    purposes: frozenset[str]
    classes: frozenset[str]

    def covers_entity(self, device: str) -> bool:
        """True when the wildcard is granted or `device` is named exactly."""
        return ENTITY_WILDCARD in self.entities or device in self.entities


# ---- the request ----

class Scope(BaseModel):
    """The entity a request is about: a device, optionally one of its interfaces (D-04)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    device: str = Field(min_length=1, max_length=_MAX_TOKEN_LENGTH, pattern=_TOKEN_PATTERN)
    interface: str | None = Field(default=None, min_length=1, max_length=_MAX_TOKEN_LENGTH,
                                  pattern=_TOKEN_PATTERN)


class ContextRequest(BaseModel):
    """One context request (CTX-01). Unknown fields, unknown values and incoherent combinations are
    refused by shape. There is no requester field: the requester is the token's `sub` (D-02)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    purpose: _Purpose
    scope: Scope
    intent_id: str | None = Field(default=None, min_length=1, max_length=_MAX_INTENT_LENGTH,
                                  pattern=_INTENT_PATTERN)
    as_of: str | None = Field(default=None, max_length=_MAX_OFFSET_LENGTH, pattern=_OFFSET_PATTERN)
    known_at: str | None = Field(default=None, max_length=_MAX_OFFSET_LENGTH, pattern=_OFFSET_PATTERN)
    classes: list[_ContextClass] = Field(min_length=1, max_length=len(CONTEXT_CLASSES))
    max_depth: int | None = Field(default=None, ge=0, le=MAX_DEPTH)
    relationship_types: list[_RelationshipType] | None = Field(
        default=None, min_length=1, max_length=len(RELATIONSHIP_TYPES))
    freshness: str | None = Field(default=None, max_length=_MAX_OFFSET_LENGTH, pattern=_OFFSET_PATTERN)

    @model_validator(mode="after")
    def _coherent(self):
        if len(set(self.classes)) != len(self.classes):
            raise ValueError("a context class is repeated")
        if self.relationship_types is not None and len(set(self.relationship_types)) != len(self.relationship_types):
            raise ValueError("a relationship type is repeated")
        # A traversal request names its depth and relationship types (CTX-08), and only then.
        topology = "topology" in self.classes
        named = (self.max_depth is not None, self.relationship_types is not None)
        if topology and not all(named):
            raise ValueError("topology needs max_depth and relationship_types")
        if not topology and any(named):
            raise ValueError("max_depth and relationship_types need the topology class")
        # Goals and placements come from an intent's binding (D-28).
        goals = "goals" in self.classes
        if goals and self.intent_id is None:
            raise ValueError("goals needs intent_id")
        if not goals and self.intent_id is not None:
            raise ValueError("intent_id needs the goals class")
        if "observations" in self.classes and not goals:
            raise ValueError("observations needs the goals class")
        return self


def authorize(request: ContextRequest, authority: Authority) -> str | None:
    """None when the request is inside the caller's authority, else one fixed detail (D-09).

    Entity is reported first, then purpose, then class. Pure: no I/O, so "before any upstream call"
    (D-10) is a matter of where the handler calls it."""
    if not authority.covers_entity(request.scope.device):
        return REFUSED_ENTITY
    if request.purpose not in authority.purposes:
        return REFUSED_PURPOSE
    if any(name not in authority.classes for name in request.classes):
        return REFUSED_CLASS
    return None


# ---- the situation identity ----

def _second(value: datetime | None) -> str | None:
    """The instant floored to the second in UTC, or None (meaning "latest", never "now")."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("situation instants must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def situation_id(*, requester: str, purpose: str, device: str, interface: str | None,
                 intent_id: str | None, valid_at: datetime, known_at: datetime | None,
                 classes, max_depth: int | None, relationship_types) -> str:
    """A stable identity for the question: the same inputs give the same id (CTX-03, D-11, D-12, D-27).

    A sha256 over canonical JSON (a list for the scope, never a joined string, so a device/interface
    split cannot collide), truncated to 128 bits. Both instants are floored to the second; a known_at
    of None is "latest". The inputs carry SITUATION_VERSION: Phase 4 stores these ids, so a change to
    the inputs is a version bump and never a silent edit."""
    body = {
        "v": SITUATION_VERSION,
        "requester": requester,
        "purpose": purpose,
        "scope": [device, interface],
        "intent": intent_id,
        "valid": _second(valid_at),
        "known": _second(known_at),
        "classes": sorted(classes),
        "max_depth": max_depth,
        "relationship_types": None if relationship_types is None else sorted(relationship_types),
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return "sit-" + hashlib.sha256(raw).hexdigest()[:32]


# ---- times and expiry ----

@dataclass(frozen=True)
class ResolvedTimes:
    """A request's offsets as instants: valid time, knowledge time (None is "latest"), the freshness
    limit in seconds and whether the question is about the past."""

    valid_at: datetime
    known_at: datetime | None
    limit_s: int
    historical: bool


def resolve_times(request: ContextRequest, now: datetime) -> ResolvedTimes:
    """Turn as_of, known_at and freshness into instants and a limit (D-05, D-13).

    Raises ClientInputError (HTTP 400 through the existing handler) for an offset beyond the retention
    period or a freshness outside 1 s to DEFAULT_MAX_AGE_S, so the limit can only tighten. The handler
    calls this before any upstream call (D-10). It is not a model validator: a ValueError raised inside
    a pydantic validator becomes a 422, and these are fixed 400s."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    valid_at = now if request.as_of is None else now - timedelta(seconds=window_seconds(request.as_of, "as_of"))
    known_at = None if request.known_at is None else now - timedelta(seconds=window_seconds(request.known_at, "known_at"))
    limit_s = DEFAULT_MAX_AGE_S if request.freshness is None else freshness_seconds(request.freshness)
    historical = request.as_of is not None or request.known_at is not None
    return ResolvedTimes(valid_at=valid_at, known_at=known_at, limit_s=limit_s, historical=historical)


def expiry(*, retrieved_at: datetime, observation_times, configured_evidence: bool, limit_s: int,
           historical: bool) -> tuple[datetime, str]:
    """When the answer stops being reusable as current context, and why (CTX-05).

    - historical: the retrieval time, basis "historical_request". A past answer is not reusable (D-14).
    - a current request with observations: the oldest observation time plus the limit, basis
      "oldest_observation" (D-13). Never clamped: an expiry already in the past is honest.
    - a current request with configured evidence only (topology or fluents): the retrieval time plus
      the limit, basis "configured_state". A configured fact's recordedAt is not its age (D-30).
    - nothing found: the retrieval time, basis "no_evidence"."""
    if historical:
        return retrieved_at, EXPIRY_HISTORICAL
    observation_times = list(observation_times)
    if observation_times:
        return min(observation_times) + timedelta(seconds=limit_s), EXPIRY_OBSERVATION
    if configured_evidence:
        return retrieved_at + timedelta(seconds=limit_s), EXPIRY_CONFIGURED
    return retrieved_at, EXPIRY_NO_EVIDENCE
