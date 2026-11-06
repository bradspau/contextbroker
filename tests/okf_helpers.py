"""Builders for knowledge trees in tmp_path: bundle files, raw bytes and the action catalogue file.

The files are written as text with LF line endings and a quoted version, the way the tracked
`knowledge/` directory is authored. `write_raw` writes exact bytes so a refusal test can break one rule.
"""
import json
from pathlib import Path

# One valid action template with all seven ActionTemplate fields.
TEMPLATE = {
    "template_id": "example-collect-interface-counters",
    "version": "1.0",
    "scope_pattern": "Router-*",
    "preconditions": ["interface exists in the topology"],
    "expected_outcome": "interface counters are read and recorded",
    "recovery": "none needed; the action only reads",
    "approved": True,
}


def _flow(items) -> str:
    return json.dumps(list(items))


def bundle_text(identifier: str, version: str = "1.0", *, status: str = "draft",
                title: str = "Interface error handling", entities=("Router-*",),
                purposes=("diagnosis",), ontology_classes=None,
                source_references=("repo:design/event-store.md",), linked_actions=(),
                body: str = "Check the counters on the interface.\n") -> str:
    lines = [
        "---",
        f"identifier: {identifier}",
        f"version: {json.dumps(version)}",
        f"status: {status}",
        f"title: {json.dumps(title)}",
        "scope:",
        f"  entities: {_flow(entities)}",
        f"  purposes: {_flow(purposes)}",
    ]
    if ontology_classes is not None:
        lines.append(f"  ontology_classes: {_flow(ontology_classes)}")
    lines.append(f"source_references: {_flow(source_references)}")
    if linked_actions:
        lines.append(f"linked_actions: {_flow(linked_actions)}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def write_bundle(root: Path, identifier: str, version: str = "1.0", **kwargs) -> Path:
    """Write root/bundles/<identifier>/<version>.md and return its path."""
    path = Path(root) / "bundles" / identifier / f"{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bundle_text(identifier, version, **kwargs).encode("utf-8"))
    return path


def write_raw(root: Path, identifier: str, version: str, data: bytes) -> Path:
    """Write exact bytes as root/bundles/<identifier>/<version>.md."""
    path = Path(root) / "bundles" / identifier / f"{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_catalogue(root: Path, templates) -> Path:
    """Write root/action-catalogue.yaml with the given template dicts (JSON is valid YAML)."""
    path = Path(root) / "action-catalogue.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not templates:
        path.write_bytes(b"templates: []\n")
        return path
    text = "templates:\n" + "".join(
        "  -\n" + "".join(f"    {key}: {json.dumps(value)}\n" for key, value in t.items())
        for t in templates)
    path.write_bytes(text.encode("utf-8"))
    return path


# --- Retrieval test doubles (plan 06-02): a fake embedder and Qdrant clients that need no model or network.
import hashlib
import math
import re

_WORD = re.compile(r"[a-z0-9]+")


def _word_hash(word: str) -> int:
    return int.from_bytes(hashlib.sha256(word.encode("utf-8")).digest()[:4], "big")


class FakeEmbedder:
    """A deterministic embedder: dense is an L2-normalised hashed bag of lower-cased words, sparse is word-hash counts.

    `documents` counts the texts embedded by embed_documents, so a test can show a call was or was not made.
    """

    dim = 16
    fingerprint = "fake:16"

    def __init__(self) -> None:
        self.documents = 0

    @staticmethod
    def _dense(text: str) -> list[float]:
        vector = [0.0] * FakeEmbedder.dim
        for word in _WORD.findall(text.lower()):
            vector[_word_hash(word) % FakeEmbedder.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vector))
        return [x / norm for x in vector] if norm else vector

    @staticmethod
    def _sparse(text: str):
        from okf_retrieval.embedder import Sparse
        counts: dict[int, float] = {}
        for word in _WORD.findall(text.lower()):
            key = _word_hash(word)
            counts[key] = counts.get(key, 0.0) + 1.0
        keys = sorted(counts)
        return Sparse(indices=tuple(keys), values=tuple(counts[k] for k in keys))

    def embed_documents(self, texts):
        self.documents += len(texts)
        return [(self._dense(t), self._sparse(t)) for t in texts]

    def embed_query(self, text):
        return self._dense(text), self._sparse(text)


def local_client():
    """An in-process Qdrant client: the production query code runs unmodified, with no server."""
    from qdrant_client import QdrantClient
    return QdrantClient(":memory:")


def closed_client():
    """A real client pointed at a closed port: constructed without a call, raises when used."""
    from okf_retrieval.index import make_client
    return make_client("http://127.0.0.1:1", None, 1)
