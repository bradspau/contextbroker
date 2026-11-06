"""The governed-knowledge vocabulary and the rules that decide whether a bundle applies to a request.

This module is pure: standard library plus the public `version_key` of the action catalogue, no I/O.
It must never import `data_contracts.context_contract` (which imports this module).

Applicability is decided here, from the declared scope only (D-43). A retrieval index only ranks
candidates; no score, similarity or rank is an argument of any function below, so none can make a
bundle applicable, true or approved. Only the latest version of an identifier is ever a candidate, and
an identifier whose latest version is withdrawn has no candidate at all (D-44).

`Device` is the context-graph node class of a device node (`Router-A`); it is not an OWL class, because
devices exist in the graph only as the `:onDevice` literal. `Interface` is also a TBox class.
"""
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from artefact_governance.action_catalogue import version_key

# What a bundle's status can be. The broker reads the field and never sets it (D-41). A withdrawn
# bundle is never returned.
STATUSES = ("draft", "reviewed", "approved", "withdrawn")
RETURNED_STATUSES = ("draft", "reviewed", "approved")

# The classes a bundle can name in its scope, and the class of each graph node type.
ONTOLOGY_CLASSES = ("Device", "Interface")
NODE_CLASSES = {"device": "Device", "interface": "Interface"}

# A declared entity pattern that matches every entity. Equal to context_contract.ENTITY_WILDCARD (a test
# pins them equal).
ENTITY_ANY = "*"

# What a knowledge retrieval can have done: answered, could not be asked, or not asked because the
# request names a past time and knowledge is current only.
RETRIEVAL_OUTCOMES = ("answered", "unavailable", "not_current")

# How many ranked candidates are asked for, and how many applicable references are returned.
KNOWLEDGE_CANDIDATES = 20
KNOWLEDGE_RESULT_CAP = 10

# The source system name on every knowledge provenance record.
KNOWLEDGE_SOURCE = "okf"

# Always used with fullmatch. An entity pattern is the whole wildcard, an exact token, or a token with one
# trailing `*` as a prefix; there is no `?`, no bracket form and no fnmatch.
IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9.\-]{2,63}")
ENTITY_PATTERN_RE = re.compile(r"\*|[A-Za-z0-9_\-/]{1,128}\*?")
SOURCE_REFERENCE_RE = re.compile(r"(repo|ontology|metric):[A-Za-z0-9_./#\-]{1,200}")
TITLE_RE = re.compile(r"[ -~]{1,120}")


@dataclass(frozen=True)
class KnowledgeScope:
    """What a bundle declares it is about. An empty `ontology_classes` means no constraint."""

    entities: tuple[str, ...]
    purposes: tuple[str, ...]
    ontology_classes: tuple[str, ...]


@dataclass(frozen=True)
class Bundle:
    """One version of one governed artefact, as read from its file.

    `content_hash` is `sha256:` plus 64 lower-case hex over the exact bytes of the file. `embedding_text`
    is the title, a newline and the body; it is held in memory for indexing only and no route returns it.
    """

    identifier: str
    version: str
    status: str
    title: str
    scope: KnowledgeScope
    source_references: tuple[str, ...]
    linked_actions: tuple[str, ...]
    content_hash: str
    embedding_text: str


@dataclass(frozen=True)
class Match:
    """Which declared parts of a bundle matched a request: the matching entity patterns (sorted), the
    request purpose, and the node class when the bundle declares ontology classes (else None)."""

    entities: tuple[str, ...]
    purpose: str
    ontology_class: str | None


def node_of(device: str, interface: str | None) -> tuple[str, str]:
    """The node id and node type of a request scope: `Router-A` is a device node and
    `Router-A/GigabitEthernet0/0/0` an interface node."""
    if interface is None:
        return device, "device"
    return f"{device}/{interface}", "interface"


def entity_matches(pattern: str, name: str) -> bool:
    """`*` matches any name; a pattern ending in `*` matches a name that starts with the part before it;
    otherwise the name must be equal. Case-sensitive."""
    if pattern == ENTITY_ANY:
        return True
    if pattern.endswith("*"):
        return name.startswith(pattern[:-1])
    return name == pattern


def _fits_purpose_and_class(bundle: Bundle, node_type: str, purpose: str) -> bool:
    if purpose not in bundle.scope.purposes:
        return False
    declared = bundle.scope.ontology_classes
    return not declared or NODE_CLASSES[node_type] in declared


def applicable(bundle: Bundle, *, device: str, interface: str | None, purpose: str) -> Match | None:
    """The match when the bundle applies to the request, else None (D-43).

    None when the status is withdrawn, when no entity pattern matches the device or the node id, when the
    purpose is not declared, or when ontology classes are declared and the node's class is not among them.
    """
    if bundle.status == "withdrawn":
        return None
    node_id, node_type = node_of(device, interface)
    if not _fits_purpose_and_class(bundle, node_type, purpose):
        return None
    matched = sorted({p for p in bundle.scope.entities
                      if entity_matches(p, device) or entity_matches(p, node_id)})
    if not matched:
        return None
    ontology_class = NODE_CLASSES[node_type] if bundle.scope.ontology_classes else None
    return Match(entities=tuple(matched), purpose=purpose, ontology_class=ontology_class)


def withheld_by_authority(bundle: Bundle, *, device: str, interface: str | None, purpose: str,
                          token_entities: frozenset[str]) -> bool:
    """True exactly when the bundle's purpose and ontology class fit the request but none of its entity
    patterns matches any entity in the caller's token (D-48). Never true when the token holds the
    wildcard, when the bundle applies, or when it is withdrawn."""
    if ENTITY_ANY in token_entities or bundle.status == "withdrawn":
        return False
    if applicable(bundle, device=device, interface=interface, purpose=purpose) is not None:
        return False
    _, node_type = node_of(device, interface)
    if not _fits_purpose_and_class(bundle, node_type, purpose):
        return False
    return not any(entity_matches(pattern, entity)
                   for pattern in bundle.scope.entities for entity in token_entities)


def latest_per_identifier(bundles: Iterable[Bundle]) -> dict[str, Bundle]:
    """The highest version of each identifier. An identifier whose latest version is withdrawn is absent,
    so an older version is never served in its place (D-44). Raises ValueError when two versions of one
    identifier compare equal (`1.0` and `1.00`)."""
    by_identifier: dict[str, list[Bundle]] = defaultdict(list)
    for bundle in bundles:
        by_identifier[bundle.identifier].append(bundle)
    latest: dict[str, Bundle] = {}
    for identifier, versions in by_identifier.items():
        keyed = sorted(versions, key=lambda b: version_key(b.version))
        if len(keyed) > 1 and version_key(keyed[-1].version) == version_key(keyed[-2].version):
            raise ValueError("duplicate version")
        if keyed[-1].status != "withdrawn":
            latest[identifier] = keyed[-1]
    return latest
