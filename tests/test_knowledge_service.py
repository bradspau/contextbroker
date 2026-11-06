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


# ---- Task 2: every fault is an unavailable answer, a stale index changes nothing --------------------

import logging
import re
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from qdrant_client import models

from broker_engine import health, main
from okf_retrieval.embedder import EmbedderUnavailable
from okf_retrieval.index import point_id
from okf_retrieval.service import CANDIDATE_DROPPED, RETRIEVAL_FAILED, STARTUP_FAILED
from tests.okf_helpers import closed_client

SECRET = "canary99x"
REPO_KNOWLEDGE = Path(__file__).resolve().parent.parent / "knowledge"


def own_records(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "okf_retrieval.service"]


class Refusing:
    def __call__(self):
        raise EmbedderUnavailable("embedding models not present")


def broken_tree(tmp_path, kind):
    root = tmp_path / "broken"
    if kind == "no-bundles-dir":
        root.mkdir()
    else:
        make_tree(root)
        (root / "action-catalogue.yaml").write_bytes(b"templates: not-a-list\n")
    return root


@pytest.mark.parametrize("kind", ["closed-client", "embedder", "no-bundles-dir", "bad-catalogue", "applicable"])
def test_a_fault_gives_unavailable_one_fixed_log_line_and_no_secret(tmp_path, caplog, monkeypatch, kind):
    root = make_tree(tmp_path / "k")
    service = service_for(root)
    if kind == "closed-client":
        service = KnowledgeService(root=root, client_factory=closed_client, embedder_factory=FakeEmbedder)
    elif kind == "embedder":
        service = KnowledgeService(root=root, client_factory=local_client, embedder_factory=Refusing())
    elif kind in ("no-bundles-dir", "bad-catalogue"):
        service = service_for(broken_tree(tmp_path, kind))
    else:
        def boom(*args, **kwargs):
            raise RuntimeError(SECRET + " http://127.0.0.1:1")
        monkeypatch.setattr(svc, "applicable", boom)
    with caplog.at_level(logging.WARNING):
        got = ask(service)
    assert got == UNAVAILABLE and got.references == ()
    lines = own_records(caplog)
    assert len(lines) == 1
    assert re.fullmatch(RETRIEVAL_FAILED + r": [A-Za-z]+", lines[0])
    assert "127.0.0.1" not in caplog.text and SECRET not in caplog.text and "port" not in caplog.text


def test_a_load_failure_is_kept_for_the_life_of_the_service(tmp_path):
    root = tmp_path / "late"
    root.mkdir()
    service = service_for(root)
    assert ask(service) == UNAVAILABLE
    make_tree(root)
    assert ask(service) == UNAVAILABLE


def test_a_qdrant_that_comes_back_is_used_on_the_next_request(tmp_path):
    root = make_tree(tmp_path / "k")
    state = {"down": True}
    client = local_client()

    def embedder():
        if state["down"]:
            raise EmbedderUnavailable("embedding models not present")
        return FakeEmbedder()

    service = KnowledgeService(root=root, client_factory=lambda: client, embedder_factory=embedder)
    assert ask(service) == UNAVAILABLE
    state["down"] = False
    assert ask(service).outcome == "answered"


def collection_of(client):
    return [c.name for c in client.get_collections().collections][0]


def built(tmp_path, caplog=None):
    root = make_tree(tmp_path / "k")
    client, embedder = local_client(), FakeEmbedder()
    service = service_for(root, embedder, client)
    assert ask(service).outcome == "answered"
    return service, client, embedder


def ghost_point(embedder, identifier, version, content_hash="sha256:" + "a" * 64):
    dense, sparse = embedder.embed_documents(["interface errors on router"])[0]
    return models.PointStruct(
        id=point_id(identifier, version),
        vector={"dense": dense, "sparse": models.SparseVector(indices=list(sparse.indices),
                                                              values=list(sparse.values))},
        payload={"identifier": identifier, "version": version, "content_hash": content_hash})


def test_a_changed_hash_in_the_index_drops_that_candidate_and_logs_it_once(tmp_path, caplog):
    service, client, _ = built(tmp_path)
    forged = "sha256:" + "0" * 64
    client.set_payload(collection_name=collection_of(client), payload={"content_hash": forged},
                       points=[point_id("interface-error", "1.0")])
    with caplog.at_level(logging.DEBUG):
        got = ask(service)
    assert {r.bundle.identifier for r in got.references} == {"latency"}
    assert own_records(caplog) == [CANDIDATE_DROPPED]
    assert forged not in caplog.text and "interface-error" not in caplog.text


def test_an_unknown_identifier_and_an_old_version_in_the_index_are_dropped(tmp_path, caplog):
    service, client, embedder = built(tmp_path)
    client.upsert(collection_name=collection_of(client), wait=True, points=[
        ghost_point(embedder, "ghost-bundle", "1.0"), ghost_point(embedder, "interface-error", "0.9")])
    with caplog.at_level(logging.DEBUG):
        got = ask(service)
    assert {r.bundle.identifier for r in got.references} == {"interface-error", "latency"}
    assert all(r.bundle.version == "1.0" for r in got.references)
    assert own_records(caplog) == [CANDIDATE_DROPPED]
    assert "ghost-bundle" not in caplog.text and "0.9" not in caplog.text


def test_twelve_applicable_bundles_give_ten_references_and_truncated(tmp_path):
    root = make_tree(tmp_path / "k", extra=10)
    got = ask(service_for(root))
    assert len(got.references) == 10 and got.truncated is True
    assert [r.rank for r in got.references] == list(range(1, 11))


def test_ten_applicable_bundles_are_not_truncated(tmp_path):
    root = make_tree(tmp_path / "k", extra=8)
    got = ask(service_for(root))
    assert len(got.references) == 10 and got.truncated is False


def test_a_tree_with_no_bundles_answers_at_once_and_builds_no_client(tmp_path):
    root = tmp_path / "empty"
    (root / "bundles").mkdir(parents=True)
    calls = []
    service = KnowledgeService(root=root, client_factory=lambda: calls.append("client"),
                               embedder_factory=lambda: calls.append("embedder"))
    assert ask(service) == Retrieval("answered", (), 0, False)
    assert calls == []


class SpyEmbedder(FakeEmbedder):
    def __init__(self):
        super().__init__()
        self.queries = []

    def embed_query(self, text):
        self.queries.append(text)
        return super().embed_query(text)


def test_without_a_topic_the_query_is_derived_from_the_purpose_and_the_node_type(tmp_path):
    root = make_tree(tmp_path / "k")
    spy = SpyEmbedder()
    service = service_for(root, spy)
    ask(service, topic=None)
    ask(service, topic=None, interface=None)
    ask(service, topic="a topic of my own")
    assert spy.queries == ["diagnosis interface", "diagnosis device", "a topic of my own"]


def test_a_wiped_collection_heals_on_the_next_request(tmp_path):
    service, client, _ = built(tmp_path)
    client.delete_collection(collection_of(client))
    assert client.get_collections().collections == []
    again = ask(service)
    assert again.outcome == "answered" and len(again.references) == 2
    assert len(client.get_collections().collections) == 1


def test_two_threads_build_the_collection_once(tmp_path):
    root = make_tree(tmp_path / "k")
    embedder = FakeEmbedder()
    service = service_for(root, embedder)
    start = threading.Barrier(2)
    answers = []

    def run():
        start.wait()
        answers.append(ask(service))

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert [a.outcome for a in answers] == ["answered", "answered"]
    assert embedder.documents == 3


def test_startup_never_raises_and_logs_the_class_only(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("BROKER_KNOWLEDGE_PATH", str(REPO_KNOWLEDGE))
    with caplog.at_level(logging.DEBUG):
        get_service().startup()
    assert own_records(caplog) == [STARTUP_FAILED + ": EmbedderUnavailable"]
    assert str(tmp_path) not in caplog.text


def test_startup_syncs_the_index_when_it_can(tmp_path):
    root = make_tree(tmp_path / "k")
    client, embedder = local_client(), FakeEmbedder()
    service_for(root, embedder, client).startup()
    assert len(client.get_collections().collections) == 1 and embedder.documents == 3


def test_startup_with_a_missing_tree_logs_once_and_returns(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        service_for(tmp_path / "nowhere").startup()
    assert own_records(caplog) == [STARTUP_FAILED + ": LoadError"]


def _answering(url, **kwargs):
    r = MagicMock()
    r.status_code = 200
    return r


def test_the_api_starts_without_qdrant_or_models_and_resets_the_service_on_exit(monkeypatch, caplog):
    monkeypatch.setenv("BROKER_KNOWLEDGE_PATH", str(REPO_KNOWLEDGE))
    health._reset_cache()
    with caplog.at_level(logging.DEBUG):
        with patch("broker_engine.health.requests.get", side_effect=_answering):
            with TestClient(main.app) as c:
                assert svc._SERVICE is not None
                assert c.get("/health").status_code in (200, 503)
    assert svc._SERVICE is None
    assert STARTUP_FAILED + ": EmbedderUnavailable" in own_records(caplog)
    health._reset_cache()
