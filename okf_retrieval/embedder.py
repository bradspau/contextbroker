"""The embedder contract: a dense vector and a sparse BM25 vector for a text.

Nothing here runs at import time beyond defining names. The real embedder (FastEmbedEmbedder) is added
beside these names and imports its model library only when it is built.
"""
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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
