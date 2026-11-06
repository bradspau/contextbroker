"""The Qdrant index ranks candidates and carries identity only (plan 06-02, OKF-02, D-35, D-36, D-38, D-43)."""
import re

from qdrant_client import models

from okf_retrieval.embedder import Embedder
from okf_retrieval.index import (Candidate, IndexEntry, PAYLOAD_KEYS, QdrantIndex, collection_name, point_id)
from tests.okf_helpers import FakeEmbedder, local_client

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
