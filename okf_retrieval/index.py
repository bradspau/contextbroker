"""The Qdrant index: it ranks candidates and carries identity only (D-35, D-38, D-43).

The collection holds a dense and a sparse vector per entry and a payload of exactly identifier, version and
content hash. No text, scope or status is stored, and no filter is passed to Qdrant: the loaded files decide
what applies (plan 06-05), the index only orders what might.
"""
import hashlib
import json
import uuid
import warnings
from dataclasses import dataclass

from qdrant_client import QdrantClient, models

from okf_retrieval.embedder import Embedder

COLLECTION_PREFIX = "knowledge-"
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "sparse"
PAYLOAD_KEYS = ("identifier", "version", "content_hash")


@dataclass(frozen=True)
class IndexEntry:
    identifier: str
    version: str
    content_hash: str
    text: str


@dataclass(frozen=True)
class Candidate:
    """A ranked candidate: it names an artefact and nothing more (no score, no text)."""

    identifier: str
    version: str
    content_hash: str


def collection_name(fingerprint: str, dim: int, entries) -> str:
    """`knowledge-` plus 12 hex over the embedder, the dimension and the sorted identity triples."""
    triples = sorted([e.identifier, e.version, e.content_hash] for e in entries)
    canonical = json.dumps([fingerprint, dim, triples], sort_keys=True, separators=(",", ":"))
    return COLLECTION_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def point_id(identifier: str, version: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identifier + "@" + version))


def make_client(url: str, api_key: str | None, timeout: int) -> QdrantClient:
    """A client that makes no call at construction.

    The hop to Qdrant is plain HTTP on the compose network, so the one insecure-connection warning about the
    key is dropped; no other warning is. The key and the URL are never logged or formatted here.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Api key is used with an insecure connection",
                                category=UserWarning)
        return QdrantClient(url=url, api_key=api_key, timeout=timeout, prefer_grpc=False,
                            check_compatibility=False)


def _sparse(vector) -> models.SparseVector:
    return models.SparseVector(indices=list(vector.indices), values=list(vector.values))


class QdrantIndex:
    def __init__(self, client, embedder: Embedder) -> None:
        self._client = client
        self._embedder = embedder

    def ensure(self, entries) -> str:
        """Return the name of a collection built from these entries, building it when it is absent.

        The name encodes the content, so an existing collection is current by construction. A build that
        fails after the create deletes the collection and re-raises the original exception, so a half-built
        collection is never taken as current. After a successful build every other `knowledge-` collection
        is deleted and no other collection is touched.
        """
        entries = list(entries)
        name = collection_name(self._embedder.fingerprint, self._embedder.dim, entries)
        if self._client.collection_exists(name):
            return name
        try:
            self._build(name, entries)
        except BaseException:
            try:
                self._client.delete_collection(name)
            except Exception:
                pass
            raise
        for existing in self._client.get_collections().collections:
            if existing.name.startswith(COLLECTION_PREFIX) and existing.name != name:
                self._client.delete_collection(existing.name)
        return name

    def _build(self, name: str, entries) -> None:
        self._client.create_collection(
            collection_name=name,
            vectors_config={DENSE_VECTOR: models.VectorParams(size=self._embedder.dim,
                                                              distance=models.Distance.COSINE)},
            sparse_vectors_config={SPARSE_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)})
        vectors = self._embedder.embed_documents([e.text for e in entries])
        points = [
            models.PointStruct(
                id=point_id(e.identifier, e.version),
                vector={DENSE_VECTOR: dense, SPARSE_VECTOR: _sparse(sparse)},
                payload={"identifier": e.identifier, "version": e.version, "content_hash": e.content_hash})
            for e, (dense, sparse) in zip(entries, vectors)]
        self._client.upsert(collection_name=name, points=points, wait=True)

    def search(self, name: str, text: str, limit: int) -> list[Candidate]:
        """Hybrid query: dense and sparse prefetches fused by RRF. Never filtered."""
        dense, sparse = self._embedder.embed_query(text)
        response = self._client.query_points(
            collection_name=name,
            prefetch=[
                models.Prefetch(query=dense, using=DENSE_VECTOR, limit=limit),
                models.Prefetch(query=_sparse(sparse), using=SPARSE_VECTOR, limit=limit)],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
            with_payload=list(PAYLOAD_KEYS))
        ranked = sorted(response.points, key=lambda p: (-p.score, p.payload["identifier"]))
        return [Candidate(p.payload["identifier"], p.payload["version"], p.payload["content_hash"])
                for p in ranked]
