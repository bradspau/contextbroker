import importlib

import pytest

from broker_engine import sparql_resolver, tsdb_joiner


@pytest.fixture(autouse=True)
def _restore_modules(monkeypatch):
    yield
    monkeypatch.undo()
    importlib.reload(sparql_resolver)
    importlib.reload(tsdb_joiner)


def test_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("FUSEKI_QUERY_ENDPOINT", raising=False)
    monkeypatch.delenv("VM_QUERY_ENDPOINT", raising=False)
    importlib.reload(sparql_resolver)
    importlib.reload(tsdb_joiner)
    assert sparql_resolver.FUSEKI_QUERY_ENDPOINT == "http://localhost:3030/network-dataset/query"
    assert tsdb_joiner.VM_QUERY_ENDPOINT == "http://localhost:8428/api/v1/query"


def test_env_overrides_endpoints(monkeypatch):
    monkeypatch.setenv("FUSEKI_QUERY_ENDPOINT", "http://fuseki:3030/network-dataset/query")
    monkeypatch.setenv("VM_QUERY_ENDPOINT", "http://victoriametrics:8428/api/v1/query")
    importlib.reload(sparql_resolver)
    importlib.reload(tsdb_joiner)
    assert sparql_resolver.FUSEKI_QUERY_ENDPOINT == "http://fuseki:3030/network-dataset/query"
    assert tsdb_joiner.VM_QUERY_ENDPOINT == "http://victoriametrics:8428/api/v1/query"
