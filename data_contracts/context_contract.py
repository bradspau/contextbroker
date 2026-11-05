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


# ---- the response ----
#
# Wire-neutral models (D-06, D-21): the response is defined once here and the HTTP layer only returns
# it. Times are ISO 8601 text, as ContextEnvelope.to_dict renders them. The JSON is the purpose-built
# nodes/edges form; JSON-LD and NGSI-LD views are Phase 7 and need no change to this content. Models
# are built by the broker, so they are extra="forbid" but not strict. Every string that reaches a
# caller is a pinned metric name, a graph IRI local name or a fixed reason, never server text.

_EXPIRY_BASES = (EXPIRY_HISTORICAL, EXPIRY_OBSERVATION, EXPIRY_CONFIGURED, EXPIRY_NO_EVIDENCE)
_FLUENT_KINDS = ("goal", "placement", "link")
_NODE_TYPES = ("device", "interface")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Provenance(_Out):
    """Where one piece of evidence came from; mirrors ProvenanceRecord.to_dict (CTX-06, D-20)."""

    source_system: str
    reference: str = Field(description="A pinned metric or series name, a fluent IRI or a component name "
                                       "and version built by the broker, never text from an upstream answer.")
    observed_at: str | None
    retrieved_at: str | None


class QualityOut(_Out):
    """Mirrors the quality block of ContextEnvelope.to_dict."""

    stale: bool
    missing: bool
    conflicts: list[str]


class EnvelopeOut(_Out):
    """One observation envelope: exactly the ContextEnvelope.to_dict fields, plus the pinned metric short
    name and the Context Graph node it belongs to. Provenance is a list of records."""

    metric: str = Field(description="The pinned metric short name.")
    node: str = Field(description="The Context Graph node id, device/interface.")
    entity_id: str
    state: str
    value: float | str | None
    source: str
    observed_at: str | None
    quality: QualityOut
    provenance: list[Provenance]


class FluentValue(_Out):
    """One goal, placement or link fluent with its valid and recorded interval (D-24)."""

    context_class: Literal[CONTEXT_CLASSES] = Field(alias="class")
    kind: Literal[_FLUENT_KINDS]
    subject: str = Field(description="The metric short name for a goal or placement, the link IRI local "
                                     "name for a link.")
    value: float | str | list[str] | None = Field(
        description="A float for a goal, a node id for a placement, a two-item list of node ids for a link.")
    in_force: bool
    valid_from: str | None
    valid_to: str | None
    recorded_at: str | None
    superseded_at: str | None
    provenance: Provenance


class ClassEligibility(_Out):
    """The eligibility of one requested context class, with its own fixed reasons (D-18)."""

    result: Literal[RESULTS]
    reasons: list[str]


class Eligibility(_Out):
    """A fitness label for the evidence: eligible, ineligible or gap, with fixed-string reasons per
    requested class. It grants no execution authority (the action-readiness check is a separate
    component) and is kept apart from the verdict: it never feeds it (D-19)."""

    result: Literal[RESULTS]
    classes: dict[Literal[CONTEXT_CLASSES], ClassEligibility]
    reasons: list[str]


class GraphNode(_Out):
    """A device or interface. `id` is the device name, or device/interface (D-25)."""

    id: str
    type: Literal[_NODE_TYPES]
    device: str
    interface: str | None
    graph_ref: str | None = Field(description="The interface IRI local name, or None for a device.")
    depth: int = Field(ge=0, description="Hops from the scope node.")


class GraphEdge(_Out):
    """A relationship between two nodes. `link` and `fluent` are IRI local names on connected_to."""

    from_: str = Field(alias="from")
    to: str
    type: Literal[RELATIONSHIP_TYPES]
    link: str | None = None
    fluent: str | None = None


class GraphLimits(_Out):
    """Which limits cut the traversal result: authority, depth, node_cap or relationship_types (D-23, D-26)."""

    requested_depth: int
    max_depth: int
    node_cap: int
    relationship_types: list[Literal[RELATIONSHIP_TYPES]]
    cut_by: list[Literal[CUT_REASONS]]


class ContextGraph(_Out):
    """The entities and relationships in scope as nodes and edges, with the limits that applied (CTX-07)."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    limits: GraphLimits


class Situation(_Out):
    """The identity and resolved inputs of the question (CTX-03). `requester` is the token's sub."""

    id: str
    requester: str
    purpose: Literal[PURPOSES]
    scope: Scope
    intent_id: str | None
    classes: list[Literal[CONTEXT_CLASSES]]
    max_depth: int | None
    relationship_types: list[Literal[RELATIONSHIP_TYPES]] | None
    valid_at: str
    known_at: str | None
    retrieved_at: str
    historical: bool


class ContextResponse(_Out):
    """The answer to one context request: situation, eligibility, expiry, evidence and graph (CTX-03 to CTX-07)."""

    situation: Situation
    eligibility: Eligibility
    expires_at: str = Field(description="When the answer stops being reusable as current context.")
    expiry_basis: Literal[_EXPIRY_BASES]
    fluent_values: list[FluentValue]
    envelopes: list[EnvelopeOut]
    graph: ContextGraph | None = Field(description="Present only when the topology class was requested.")
    verdict: dict | None = Field(
        description="Present only when an intent is named with the goals and observations classes. It comes "
                    "from the read-only TMF921 assess, never reads eligibility and can disagree with it.")
