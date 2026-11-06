"""The knowledge service: the index ranks, the loaded files decide (plan 06-05, OKF-01, OKF-02, D-43, D-44, D-47, D-48)."""
import os

import pytest

from okf_retrieval import service as svc
from okf_retrieval.index import IndexEntry, QdrantIndex
from okf_retrieval.service import (ApplicableRef, Retrieval, UNAVAILABLE, KnowledgeService, default_query,
                                   get_service, reset_service, set_service)
from tests.okf_helpers import FakeEmbedder, local_client, write_bundle, write_catalogue

DECOY_TEXT = "interface errors on router counters interface errors"


def make_tree(root, *, decoy=True, extra=0):
    write_bundle(root, "interface-error", title="Interface error handling", entities=("Router-*",),
                 purposes=("diagnosis",), ontology_classes=("Interface",),
                 body="Check the interface error counters on the router.\n")
    write_bundle(root, "latency", title="Latency goals", entities=("Router-*",),
                 purposes=("diagnosis",), ontology_classes=("Interface",),
                 body="A latency goal limits delay between two sites.\n")
    if decoy:
        write_bundle(root, "decoy", title="Decoy", entities=("Switch-Decoy-9",),
                     purposes=("diagnosis",), body=DECOY_TEXT + "\n")
    for n in range(extra):
        write_bundle(root, f"filler-{n:02d}", title=f"Filler {n}", entities=("Router-*",),
                     purposes=("diagnosis",), body=f"Filler bundle number {n} about counters.\n")
    write_catalogue(root, [])
    return root


def service_for(root, embedder=None, client=None):
    embedder = embedder or FakeEmbedder()
    client = client or local_client()
    return KnowledgeService(root=root, client_factory=lambda: client, embedder_factory=lambda: embedder)


def ask(service, *, interface="GigabitEthernet0/0/0", topic=DECOY_TEXT, token=frozenset({"*"}),
        device="Router-A", purpose="diagnosis"):
    return service.retrieve(device=device, interface=interface, purpose=purpose, topic=topic,
                            token_entities=token)


def test_the_index_first_ranked_decoy_is_never_returned(tmp_path):
    root = make_tree(tmp_path / "k")
    got = ask(service_for(root))
    assert got.outcome == "answered"
    assert {r.bundle.identifier for r in got.references} == {"interface-error", "latency"}
    assert [r.rank for r in got.references] == [1, 2]
    for ref in got.references:
        assert isinstance(ref, ApplicableRef)
        assert ref.match.purpose == "diagnosis" and ref.match.ontology_class == "Interface"
        assert ref.match.entities == ("Router-*",)
        assert ref.actions == () and ref.unresolved_actions == 0
    assert got.withheld == 0 and got.truncated is False


def test_the_index_alone_puts_the_decoy_first(tmp_path):
    from okf_retrieval.loader import load_bundles
    root = make_tree(tmp_path / "k")
    latest = load_bundles(root).latest
    embedder, client = FakeEmbedder(), local_client()
    index = QdrantIndex(client, embedder)
    name = index.ensure([IndexEntry(b.identifier, b.version, b.content_hash, b.embedding_text)
                         for b in latest.values()])
    assert index.search(name, DECOY_TEXT, 20)[0].identifier == "decoy"


def test_a_scoped_token_counts_what_it_cannot_see_without_naming_it(tmp_path):
    root = make_tree(tmp_path / "k")
    got = ask(service_for(root), token=frozenset({"Router-A"}))
    assert got.withheld == 1
    assert {r.bundle.identifier for r in got.references} == {"interface-error", "latency"}
    assert not hasattr(got, "withheld_identifiers")


def test_a_wildcard_token_never_withholds(tmp_path):
    root = make_tree(tmp_path / "k")
    assert ask(service_for(root), token=frozenset({"*"})).withheld == 0


def test_the_references_hold_no_score(tmp_path):
    root = make_tree(tmp_path / "k")
    got = ask(service_for(root))
    for ref in got.references:
        assert set(vars(ref)) == {"bundle", "match", "rank", "actions", "unresolved_actions"}
    assert set(vars(got)) == {"outcome", "references", "withheld", "truncated"}


def test_the_process_service_is_one_object_and_can_be_replaced_and_reset(monkeypatch):
    first = get_service()
    assert get_service() is first
    other = KnowledgeService(root=first._root, client_factory=lambda: None, embedder_factory=lambda: None)
    set_service(other)
    assert get_service() is other
    reset_service()
    assert get_service() is not other


def test_building_the_default_service_does_no_io(monkeypatch):
    monkeypatch.setenv("BROKER_QDRANT_URL", "http://127.0.0.1:1")
    built = get_service()
    assert isinstance(built, KnowledgeService)
    assert built._client is None and built._embedder is None and built._files is None


def test_every_test_starts_with_no_service_and_no_models(monkeypatch):
    assert svc._SERVICE is None
    assert not os.path.exists(os.environ["BROKER_EMBED_MODEL_DIR"])


def test_the_unavailable_answer_is_empty():
    assert UNAVAILABLE == Retrieval("unavailable", (), 0, False)
    assert default_query("diagnosis", "interface") == "diagnosis interface"
