"""The closed vocabulary of semantic promotion (SEM-01).

Promotion keeps a derived record beyond the request that produced it. What is promoted is chosen by
these fixed rules only (D-57): two kinds, two named rules, no operator rule file and no per-request
flag. A conflict is a stored event that disagrees with configured state; a hypothesis is a topology
link inferred from stored neighbour-discovery events where the configured topology has none.

Rules that hold for everything built on this vocabulary:

- A promoted record never changes configured state and never counts as evidence: it feeds neither the
  eligibility, the expiry nor the verdict (D-64).
- A hypothesis is labelled "hypothesis" with state "inferred", so no consumer can take it for
  configured topology (D-63).
- A record points at what supported it (the stored situation and the event identifiers), and a
  support that has been evicted is marked, never left dangling (D-60).

This module is pure and standard library only. It must never import the response models: they import
this vocabulary, so the other direction would be a cycle.
"""
import hashlib
import json
import re
from types import MappingProxyType

# Bumped if the identity construction in promoted_id() ever changes: the state file stores these ids.
PROMOTION_VERSION = 1

PROMOTED_KINDS = ("conflict", "hypothesis")

RULE_REPEATED_LINK_DOWN = "repeated_link_down_vs_configured"
RULE_ADJACENCY = "adjacency_without_configured_link"
PROMOTION_RULES = (RULE_REPEATED_LINK_DOWN, RULE_ADJACENCY)
# Which kind each rule produces. Read-only, so a caller cannot add a rule at run time.
RULE_KIND = MappingProxyType({RULE_REPEATED_LINK_DOWN: "conflict", RULE_ADJACENCY: "hypothesis"})

# The source system name on the provenance record of every promoted record.
PROMOTION_SOURCE = "broker-promotion"

# What a record points at, and in what condition the target is found at read time. "withheld" is a
# situation the caller may not see: it is counted and never named.
SUPPORT_KINDS = ("situation", "event")
SUPPORT_STATES = ("present", "evicted", "withheld")
# At most this many support references are stored and returned per record.
PROMOTED_SUPPORT_MAX = 20

HYPOTHESIS_LABEL = "hypothesis"
# The same text as data_contracts.envelope.StateCategory.INFERRED.value; a test pins them equal.
HYPOTHESIS_STATE = "inferred"

# `prm-` and 32 lower-case hex digits, as promoted_id builds it. `\Z`, never `$`: a trailing newline
# must not match.
PROMOTED_ID_PATTERN = re.compile(r"^prm-[0-9a-f]{32}\Z")


def promoted_id(kind: str, rule: str, subject: tuple[str, ...]) -> str:
    """A stable identity for a promoted record: the same kind, rule and subject give the same id.

    `prm-` plus the first 32 hex digits of SHA-256 over canonical JSON (a list for the subject, never a
    joined string, so two subjects cannot collide). A conflict's subject is (device,); a hypothesis's
    subject is the two node ids, sorted. Raises ValueError for an unknown kind or rule, a rule that
    belongs to the other kind, or a subject that is not a non-empty tuple of non-empty strings."""
    if kind not in PROMOTED_KINDS:
        raise ValueError("unknown promoted kind")
    if rule not in PROMOTION_RULES:
        raise ValueError("unknown promotion rule")
    if RULE_KIND[rule] != kind:
        raise ValueError("the rule does not produce this kind")
    if (not isinstance(subject, tuple) or not subject
            or not all(isinstance(part, str) and part for part in subject)):
        raise ValueError("the subject must be a non-empty tuple of non-empty strings")
    body = {"v": PROMOTION_VERSION, "kind": kind, "rule": rule, "subject": list(subject)}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return "prm-" + hashlib.sha256(raw).hexdigest()[:32]
