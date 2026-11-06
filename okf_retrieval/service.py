"""The knowledge service: Qdrant ranks, the loaded files decide (plan 06-05).

`retrieve` asks the index for a few ranked candidates and then decides from the loaded bundle files alone
(D-43): a candidate is kept only when its identifier is loaded, its version is the latest loaded version,
its content hash equals the loaded file's hash and `applicable()` matches the request. No ranking value from
the index is read or kept, so a high similarity can never make a bundle apply (OKF-02).

Every fault becomes the outcome `unavailable` with one log line (a fixed reason and the exception class),
so the request handler is never reached by a Qdrant, embedder or file fault (D-37). Nothing here writes a
bundle, a status, the catalogue or a file, and nothing calls, schedules or records an action (OKF-03).
"""
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from artefact_governance.action_catalogue import ActionCatalogue, ActionTemplate
from broker_engine.faults import log_fault
from data_contracts.knowledge import (
    KNOWLEDGE_CANDIDATES,
    KNOWLEDGE_RESULT_CAP,
    RETRIEVAL_OUTCOMES,
    Bundle,
    Match,
    applicable,
    node_of,
    withheld_by_authority,
)
from okf_retrieval import config
from okf_retrieval.embedder import Embedder, FastEmbedEmbedder
from okf_retrieval.index import IndexEntry, QdrantIndex, make_client
from okf_retrieval.loader import BundleSet, load_bundles, load_catalogue

log = logging.getLogger(__name__)

STARTUP_FAILED = "knowledge start-up failed"
RETRIEVAL_FAILED = "knowledge retrieval failed"
CANDIDATE_DROPPED = "knowledge candidate dropped: index differs from files"


@dataclass(frozen=True)
class ApplicableRef:
    """One applicable bundle: the loaded bundle, why it matched, its rank and its approved action templates."""

    bundle: Bundle
    match: Match
    rank: int
    actions: tuple[ActionTemplate, ...]
    unresolved_actions: int


@dataclass(frozen=True)
class Retrieval:
    outcome: str
    references: tuple[ApplicableRef, ...]
    withheld: int
    truncated: bool

    def __post_init__(self) -> None:
        if self.outcome not in RETRIEVAL_OUTCOMES:
            raise ValueError("unknown retrieval outcome")


UNAVAILABLE = Retrieval("unavailable", (), 0, False)


def default_query(purpose: str, node_type: str) -> str:
    """The query text when the request names no topic (D-47)."""
    return f"{purpose} {node_type}"


def resolve_actions(catalogue: ActionCatalogue, linked: tuple[str, ...], *, device: str,
                    node_id: str) -> tuple[tuple[ActionTemplate, ...], int]:
    """The approved latest template of each declared id whose scope pattern matches the device or the node.

    `ActionCatalogue.get` keeps its meaning (approved, latest approved). Every id that does not resolve is
    only counted, never named. Templates come back in declared order (D-46, OKF-03).
    """
    resolved: list[ActionTemplate] = []
    unresolved = 0
    for template_id in linked:
        template = catalogue.get(template_id)
        if template is not None and (fnmatchcase(device, template.scope_pattern)
                                     or fnmatchcase(node_id, template.scope_pattern)):
            resolved.append(template)
        else:
            unresolved += 1
    return tuple(resolved), unresolved


@dataclass(frozen=True)
class _Files:
    bundles: BundleSet
    catalogue: ActionCatalogue


class KnowledgeService:
    def __init__(self, *, root: Path, client_factory: Callable[[], object],
                 embedder_factory: Callable[[], Embedder]) -> None:
        self._root = Path(root)
        self._client_factory = client_factory
        self._embedder_factory = embedder_factory
        self._lock = threading.Lock()
        self._files: _Files | None = None
        self._load_failure: Exception | None = None
        self._client = None
        self._embedder: Embedder | None = None

    # -- files: read once; a failure is kept for the life of the service ----------------------------

    def _loaded(self) -> _Files:
        if self._load_failure is not None:
            raise self._load_failure.with_traceback(None)
        if self._files is None:
            try:
                self._files = _Files(load_bundles(self._root), load_catalogue(self._root))
            except Exception as exc:
                self._load_failure = exc
                raise
        return self._files

    # -- index: the embedder and the collection are retried on every request ------------------------

    def _ready_index(self, files: _Files) -> tuple[QdrantIndex, str]:
        if self._embedder is None:
            self._embedder = self._embedder_factory()
        if self._client is None:
            self._client = self._client_factory()
        index = QdrantIndex(self._client, self._embedder)
        entries = [IndexEntry(b.identifier, b.version, b.content_hash, b.embedding_text)
                   for _, b in sorted(files.bundles.latest.items())]
        return index, index.ensure(entries)

    def startup(self) -> None:
        """Load the files and sync the index when it can; never raises."""
        with self._lock:
            try:
                files = self._loaded()
                if files.bundles.latest:
                    self._ready_index(files)
            except Exception as exc:
                log_fault(log, exc, STARTUP_FAILED)

    def retrieve(self, *, device: str, interface: str | None, purpose: str, topic: str | None,
                 token_entities: frozenset[str]) -> Retrieval:
        """The applicable bundles for a request, ranked in the index's fused order; never raises."""
        try:
            with self._lock:
                return self._retrieve(device, interface, purpose, topic, token_entities)
        except Exception as exc:
            log_fault(log, exc, RETRIEVAL_FAILED)
            return UNAVAILABLE

    def _retrieve(self, device, interface, purpose, topic, token_entities) -> Retrieval:
        files = self._loaded()
        latest = files.bundles.latest
        if not latest:
            return Retrieval("answered", (), 0, False)
        node_id, node_type = node_of(device, interface)
        index, name = self._ready_index(files)
        text = topic if topic is not None else default_query(purpose, node_type)
        candidates = index.search(name, text, KNOWLEDGE_CANDIDATES)

        kept: list[tuple[Bundle, Match]] = []
        withheld = 0
        dropped = False
        seen: set[str] = set()
        for candidate in candidates:
            bundle = latest.get(candidate.identifier)
            if (bundle is None or bundle.version != candidate.version
                    or bundle.content_hash != candidate.content_hash or bundle.identifier in seen):
                dropped = True
                continue
            seen.add(bundle.identifier)
            match = applicable(bundle, device=device, interface=interface, purpose=purpose)
            if match is not None:
                kept.append((bundle, match))
            elif withheld_by_authority(bundle, device=device, interface=interface, purpose=purpose,
                                       token_entities=token_entities):
                withheld += 1
        if dropped:
            log.warning(CANDIDATE_DROPPED)

        references = []
        for rank, (bundle, match) in enumerate(kept[:KNOWLEDGE_RESULT_CAP], start=1):
            actions, unresolved = resolve_actions(files.catalogue, bundle.linked_actions,
                                                  device=device, node_id=node_id)
            references.append(ApplicableRef(bundle=bundle, match=match, rank=rank, actions=actions,
                                            unresolved_actions=unresolved))
        references = tuple(references)
        return Retrieval("answered", references, withheld, len(kept) > KNOWLEDGE_RESULT_CAP)

    def close(self) -> None:
        """Close the client if one was built."""
        with self._lock:
            client, self._client = self._client, None
        close = getattr(client, "close", None)
        if close is not None:
            try:
                close()
            except Exception as exc:
                log_fault(log, exc, RETRIEVAL_FAILED)


# -- the process service: built on first use with no I/O, replaced by tests --------------------------

_LOCK = threading.Lock()
_SERVICE: KnowledgeService | None = None


def _default_service() -> KnowledgeService:
    return KnowledgeService(
        root=config.knowledge_root(),
        client_factory=lambda: make_client(config.qdrant_url(), config.qdrant_api_key(),
                                           config.QDRANT_TIMEOUT_S),
        embedder_factory=lambda: FastEmbedEmbedder(config.model_dir()))


def get_service() -> KnowledgeService:
    global _SERVICE
    with _LOCK:
        if _SERVICE is None:
            _SERVICE = _default_service()
        return _SERVICE


def set_service(service: KnowledgeService) -> None:
    global _SERVICE
    with _LOCK:
        previous, _SERVICE = _SERVICE, service
    if previous is not None and previous is not service:
        previous.close()


def reset_service() -> None:
    global _SERVICE
    with _LOCK:
        service, _SERVICE = _SERVICE, None
    if service is not None:
        service.close()
