"""The embedder contract: a dense vector and a sparse BM25 vector for a text.

Nothing here runs at import time beyond defining names. The real embedder (FastEmbedEmbedder) is added
beside these names and imports its model library only when it is built.
"""
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from okf_retrieval import config


@dataclass(frozen=True)
class Sparse:
    """A sparse vector: parallel tuples of term indices and weights."""

    indices: tuple[int, ...]
    values: tuple[float, ...]


class EmbedderUnavailable(Exception):
    """The embedding models cannot be loaded. The message is fixed; it never carries a path."""


@runtime_checkable
class Embedder(Protocol):
    """What the index needs from an embedder."""

    fingerprint: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[tuple[list[float], Sparse]]: ...

    def embed_query(self, text: str) -> tuple[list[float], Sparse]: ...


def fastembed_fingerprint() -> str:
    """Names the two models, their pinned commits and the dimension; built only from the config constants."""
    return (f"fastembed:{config.DENSE_MODEL}@{config.DENSE_REVISION}"
            f"+{config.SPARSE_MODEL}@{config.SPARSE_REVISION}:{config.DENSE_DIM}")


class FastEmbedEmbedder:
    """Dense and sparse embeddings from two models already on disk. It never downloads a model (D-36).

    The directory must hold the dense model under DENSE_SUBDIR and the sparse model under SPARSE_SUBDIR;
    when either is missing it raises EmbedderUnavailable before fastembed is imported.
    """

    dim = config.DENSE_DIM

    def __init__(self, model_dir: Path) -> None:
        model_dir = Path(model_dir)
        dense_dir = model_dir / config.DENSE_SUBDIR
        sparse_dir = model_dir / config.SPARSE_SUBDIR
        if not dense_dir.is_dir() or not sparse_dir.is_dir():
            raise EmbedderUnavailable("embedding models not present")
        from fastembed import SparseTextEmbedding, TextEmbedding  # lazy: no model library at import time
        self.fingerprint = fastembed_fingerprint()
        self._dense = TextEmbedding(config.DENSE_MODEL, specific_model_path=str(dense_dir))
        self._sparse = SparseTextEmbedding(config.SPARSE_MODEL, specific_model_path=str(sparse_dir))
        self._lock = threading.Lock()

    @staticmethod
    def _sparse_of(embedding) -> Sparse:
        return Sparse(indices=tuple(int(i) for i in embedding.indices),
                      values=tuple(float(v) for v in embedding.values))

    def embed_documents(self, texts: list[str]) -> list[tuple[list[float], Sparse]]:
        with self._lock:
            dense = [[float(x) for x in v] for v in self._dense.passage_embed(texts)]
            sparse = [self._sparse_of(e) for e in self._sparse.passage_embed(texts)]
        return list(zip(dense, sparse))

    def embed_query(self, text: str) -> tuple[list[float], Sparse]:
        with self._lock:
            dense = [float(x) for x in next(iter(self._dense.query_embed(text)))]
            sparse = self._sparse_of(next(iter(self._sparse.query_embed(text))))
        return dense, sparse
