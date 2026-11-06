"""Reads governed knowledge from files: bundles under `<root>/bundles/<identifier>/<version>.md`.

A bundle is Markdown with YAML front matter. The loader is strict, and every refusal raises `LoadError`
with one fixed reason from `LOAD_REASONS`; no file text, offset or line is ever put in an exception or a
log line. One refused file blocks its whole identifier (so an older version is never served in its place,
D-44). Nothing here writes a file: the broker reads `status` and never sets it (D-41).
"""
import hashlib
import logging
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from artefact_governance.action_catalogue import version_key
from data_contracts.context_contract import PURPOSES
from data_contracts.knowledge import (
    ENTITY_PATTERN_RE,
    IDENTIFIER_RE,
    ONTOLOGY_CLASSES,
    SOURCE_REFERENCE_RE,
    STATUSES,
    TITLE_RE,
    Bundle,
    KnowledgeScope,
    latest_per_identifier,
)

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 65536
MAX_FILES = 500
MAX_BODY_CHARS = 6000
MAX_TEMPLATES = 100
CATALOGUE_FILE = "action-catalogue.yaml"

LOAD_REASONS = (
    "not utf-8", "byte order mark", "carriage return", "too large", "symlink", "not a bundle file",
    "no front matter", "duplicate key", "not yaml", "invalid front matter", "path mismatch",
    "duplicate version", "invalid version", "no body", "body too long", "no bundles directory",
    "too many files", "invalid catalogue",
)

_MAX_LIST = 50


class LoadError(Exception):
    """A refusal with one fixed reason; the message is the reason and nothing else."""

    def __init__(self, reason: str):
        if reason not in LOAD_REASONS:
            raise ValueError("unknown load reason")
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class BundleSet:
    """The latest version of each servable identifier, and the identifiers a refused file blocked."""

    latest: Mapping[str, Bundle]
    blocked: frozenset[str]


class _DuplicateKey(Exception):
    pass


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with the same key twice (PyYAML keeps the last)."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in seen:
                    raise _DuplicateKey
                seen.add(key)
            except TypeError:
                raise yaml.YAMLError("unhashable key") from None
        return super().construct_mapping(node, deep=deep)


def _check(regex):
    def validate(value: str) -> str:
        if not regex.fullmatch(value):
            raise ValueError("invalid value")
        return value
    return validate


def _member(allowed):
    def validate(value: str) -> str:
        if value not in allowed:
            raise ValueError("invalid value")
        return value
    return validate


_Title = Annotated[str, AfterValidator(_check(TITLE_RE))]
_Identifier = Annotated[str, AfterValidator(_check(IDENTIFIER_RE))]
_Entity = Annotated[str, AfterValidator(_check(ENTITY_PATTERN_RE))]
_Source = Annotated[str, AfterValidator(_check(SOURCE_REFERENCE_RE))]
_Purpose = Annotated[str, AfterValidator(_member(PURPOSES))]
_OntologyClass = Annotated[str, AfterValidator(_member(ONTOLOGY_CLASSES))]

_STRICT = ConfigDict(extra="forbid", strict=True)


class _Scope(BaseModel):
    model_config = _STRICT
    entities: list[_Entity] = Field(min_length=1, max_length=_MAX_LIST)
    purposes: list[_Purpose] = Field(min_length=1, max_length=_MAX_LIST)
    ontology_classes: list[_OntologyClass] = Field(default_factory=list, max_length=_MAX_LIST)


class _FrontMatter(BaseModel):
    model_config = _STRICT
    identifier: _Identifier
    version: str
    status: Literal[STATUSES]
    title: _Title
    scope: _Scope
    source_references: list[_Source] = Field(min_length=1, max_length=_MAX_LIST)
    linked_actions: list[_Identifier] = Field(default_factory=list, max_length=_MAX_LIST)


def _load_yaml(text: str):
    try:
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except _DuplicateKey:
        raise LoadError("duplicate key") from None
    except yaml.YAMLError:
        raise LoadError("not yaml") from None


def _split(text: str) -> tuple[str, str]:
    """The front matter between a first line `---` and the next line `---`, and the body after it."""
    lines = text.split("\n")
    if lines[0] != "---":
        raise LoadError("no front matter")
    for index in range(1, len(lines)):
        if lines[index] == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1:])
    raise LoadError("no front matter")


def parse_bundle(path: Path, raw: bytes) -> Bundle:
    """The bundle in `raw`, the bytes of the file at `path`, or a LoadError with a fixed reason."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise LoadError("not utf-8") from None
    front_text, body = _split(text)
    data = _load_yaml(front_text)
    try:
        front = _FrontMatter.model_validate(data)
    except ValidationError:
        raise LoadError("invalid front matter") from None
    path = Path(path)
    if path.parent.name != front.identifier or path.stem != front.version:
        raise LoadError("path mismatch")
    try:
        version_key(front.version)
    except ValueError:
        raise LoadError("invalid version") from None
    body = body.strip()
    return Bundle(
        identifier=front.identifier,
        version=front.version,
        status=front.status,
        title=front.title,
        scope=KnowledgeScope(
            entities=tuple(front.scope.entities),
            purposes=tuple(front.scope.purposes),
            ontology_classes=tuple(front.scope.ontology_classes)),
        source_references=tuple(front.source_references),
        linked_actions=tuple(front.linked_actions),
        content_hash="sha256:" + hashlib.sha256(raw).hexdigest(),
        embedding_text=front.title + "\n" + body,
    )


def _read(path: Path) -> bytes:
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise LoadError("not a bundle file")
    with open(path, "rb") as handle:
        return handle.read()


def load_bundles(root: Path) -> BundleSet:
    """Read every `<root>/bundles/<identifier>/<version>.md` and keep the latest servable version of
    each identifier. A refused file blocks its identifier; withdrawn latest removes it without blocking."""
    base = Path(root) / "bundles"
    if not base.is_dir():
        raise LoadError("no bundles directory")
    parsed: dict[str, list[Bundle]] = {}
    blocked: set[str] = set()
    for directory in sorted(p for p in base.iterdir() if p.is_dir()):
        identifier = directory.name
        if not IDENTIFIER_RE.fullmatch(identifier):
            continue
        parsed[identifier] = []
        for path in sorted(directory.iterdir()):
            try:
                parsed[identifier].append(parse_bundle(path, _read(path)))
            except LoadError as exc:
                log.warning("knowledge bundle refused: %s: %s", identifier, exc.reason)
                blocked.add(identifier)
    latest: dict[str, Bundle] = {}
    for identifier, versions in parsed.items():
        if identifier in blocked:
            continue
        try:
            latest.update(latest_per_identifier(versions))
        except ValueError:
            log.warning("knowledge bundle refused: %s: %s", identifier, "duplicate version")
            blocked.add(identifier)
    return BundleSet(latest=latest, blocked=frozenset(blocked))
