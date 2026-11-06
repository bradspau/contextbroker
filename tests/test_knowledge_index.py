"""The Qdrant index ranks candidates and carries identity only (plan 06-02, OKF-02, D-35, D-36, D-38, D-43)."""
import ast
import re
import sys
import warnings
from pathlib import Path

import pytest
from qdrant_client import models

from okf_retrieval import config
from okf_retrieval.embedder import Embedder, EmbedderUnavailable, FastEmbedEmbedder, fastembed_fingerprint
from okf_retrieval.index import (COLLECTION_PREFIX, Candidate, IndexEntry, PAYLOAD_KEYS, QdrantIndex,
                                 collection_name, make_client, point_id)
from tests.okf_helpers import FakeEmbedder, closed_client, local_client

ENTRIES = [
    IndexEntry("interface-errors", "1.0", "sha256:" + "1" * 64,
               "Interface error handling\nCheck the interface error counters on the router."),
    IndexEntry("latency-goal", "1.0", "sha256:" + "2" * 64,
               "Latency goals\nA latency goal limits delay between two sites."),
    IndexEntry("topology-change", "1.0", "sha256:" + "3" * 64,
               "Topology change\nA link was added between two routers in the topology."),
    IndexEntry("decoy", "1.0", "sha256:" + "4" * 64,
               "Decoy\ninterface errors interface errors on router counters interface errors."),
]


class SpyClient:
    """Wraps a client and records every query_points call's keyword arguments."""

    def __init__(self, inner):
        self._inner = inner
        self.queries = []

    def query_points(self, **kwargs):
        self.queries.append(kwargs)
        return self._inner.query_points(**kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_fake_embedder_is_an_embedder_and_repeats_itself():
    embedder = FakeEmbedder()
    assert isinstance(embedder, Embedder)
    assert embedder.embed_query("interface errors") == embedder.embed_query("interface errors")
    assert embedder.dim == 16 and embedder.fingerprint == "fake:16"


def test_ensure_names_the_collection_and_search_returns_identity_only_candidates():
    index = QdrantIndex(local_client(), FakeEmbedder())
    name = index.ensure(ENTRIES)
    assert re.fullmatch(r"knowledge-[0-9a-f]{12}", name)
    found = index.search(name, "interface errors on router", 4)
    assert len(found) == 4
    assert all(isinstance(c, Candidate) for c in found)
    assert {c.identifier for c in found[:2]} == {"interface-errors", "decoy"}
    for candidate in found:
        assert set(vars(candidate)) == {"identifier", "version", "content_hash"}


def test_search_makes_one_hybrid_call_with_two_prefetches_rrf_and_no_filter():
    spy = SpyClient(local_client())
    index = QdrantIndex(spy, FakeEmbedder())
    name = index.ensure(ENTRIES)
    index.search(name, "interface errors on router", 4)
    assert len(spy.queries) == 1
    call = spy.queries[0]
    assert sorted(p.using for p in call["prefetch"]) == ["dense", "sparse"]
    assert len(call["prefetch"]) == 2
    assert isinstance(call["query"], models.FusionQuery)
    assert call["query"].fusion == models.Fusion.RRF
    assert call.get("query_filter") is None
    assert all(getattr(p, "filter", None) is None for p in call["prefetch"])


def test_search_limit_bounds_the_answer():
    index = QdrantIndex(local_client(), FakeEmbedder())
    name = index.ensure(ENTRIES)
    assert len(index.search(name, "interface errors on router", 2)) == 2


def test_point_id_is_a_stable_uuid_from_identifier_and_version():
    assert point_id("a", "1.0") == point_id("a", "1.0")
    assert point_id("a", "1.0") != point_id("a", "1.1")
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}", point_id("a", "1.0"))


def test_collection_name_is_a_function_of_the_entries_in_any_order():
    assert collection_name("fake:16", 16, ENTRIES) == collection_name("fake:16", 16, list(reversed(ENTRIES)))


def test_collection_name_is_stable_and_changes_with_each_input():
    base = collection_name("fake:16", 16, ENTRIES)
    assert base == collection_name("fake:16", 16, ENTRIES)
    edited = [IndexEntry("decoy", "1.0", "sha256:" + "9" * 64, "x") if e.identifier == "decoy" else e
              for e in ENTRIES]
    assert collection_name("fake:16", 16, edited) != base
    assert collection_name("fake:16", 16, ENTRIES[:3]) != base
    assert collection_name("fake:16", 16, ENTRIES + [IndexEntry("new", "1.0", "sha256:" + "5" * 64, "n")]) != base
    assert collection_name("fake:17", 16, ENTRIES) != base
    assert collection_name("fake:16", 17, ENTRIES) != base


class CountingClient(SpyClient):
    def __init__(self, inner):
        super().__init__(inner)
        self.upserts = 0

    def upsert(self, **kwargs):
        self.upserts += 1
        return self._inner.upsert(**kwargs)


def test_second_ensure_does_no_work_and_a_wiped_store_heals():
    client = CountingClient(local_client())
    embedder = FakeEmbedder()
    index = QdrantIndex(client, embedder)
    name = index.ensure(ENTRIES)
    embedded, upserts = embedder.documents, client.upserts
    assert index.ensure(ENTRIES) == name
    assert embedder.documents == embedded and client.upserts == upserts
    client.delete_collection(name)
    assert index.ensure(ENTRIES) == name
    assert len(index.search(name, "interface errors on router", 4)) == 4


def test_building_deletes_other_knowledge_collections_and_keeps_unrelated_ones():
    client = local_client()
    for stale in ("knowledge-000000000000", "other"):
        client.create_collection(collection_name=stale, vectors_config=models.VectorParams(
            size=4, distance=models.Distance.COSINE))
    name = QdrantIndex(client, FakeEmbedder()).ensure(ENTRIES)
    assert client.collection_exists("other")
    assert not client.collection_exists("knowledge-000000000000")
    assert client.collection_exists(name)


def test_the_collection_payload_holds_identity_keys_and_nothing_else():
    client = local_client()
    name = QdrantIndex(client, FakeEmbedder()).ensure(ENTRIES)
    points, _ = client.scroll(collection_name=name, limit=100, with_payload=True)
    assert len(points) == len(ENTRIES)
    for point in points:
        assert tuple(sorted(point.payload)) == tuple(sorted(PAYLOAD_KEYS))
        assert not any(w in str(point.payload) for w in ("Interface error handling", "Router-*", "draft"))


class BrokenEmbedder(FakeEmbedder):
    def embed_documents(self, texts):
        raise RuntimeError("boom")


def test_a_failed_build_leaves_no_collection():
    client = local_client()
    index = QdrantIndex(client, BrokenEmbedder())
    with pytest.raises(RuntimeError):
        index.ensure(ENTRIES)
    name = collection_name(BrokenEmbedder.fingerprint, BrokenEmbedder.dim, ENTRIES)
    assert client.collection_exists(name) is False
    assert not [c for c in client.get_collections().collections if c.name.startswith(COLLECTION_PREFIX)]


def test_a_closed_port_client_is_built_without_a_call_and_raises_when_used():
    client = closed_client()
    with pytest.raises(Exception):
        QdrantIndex(client, FakeEmbedder()).ensure(ENTRIES)


def test_make_client_raises_no_api_key_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        make_client("http://127.0.0.1:1", "abc123", 1)
    assert not [w for w in caught if "api key" in str(w.message).lower()]


def test_the_real_embedder_refuses_before_importing_fastembed(tmp_path):
    before = {m for m in sys.modules if m.startswith("fastembed")}
    with pytest.raises(EmbedderUnavailable) as raised:
        FastEmbedEmbedder(tmp_path)
    assert str(raised.value) == "embedding models not present"
    assert {m for m in sys.modules if m.startswith("fastembed")} == before
    (tmp_path / config.DENSE_SUBDIR).mkdir()
    with pytest.raises(EmbedderUnavailable):
        FastEmbedEmbedder(tmp_path)


def test_the_embedder_module_has_no_module_level_fastembed_import():
    tree = ast.parse(Path("okf_retrieval/embedder.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("fastembed") for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("fastembed")


def test_the_fastembed_fingerprint_names_both_revisions_and_is_stable():
    fingerprint = fastembed_fingerprint()
    assert config.DENSE_REVISION in fingerprint and config.SPARSE_REVISION in fingerprint
    assert fingerprint == fastembed_fingerprint()
