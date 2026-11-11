import importlib

import pytest

from broker_engine import sparql_resolver, tsdb_joiner
from ingestion import telemetry_simulator


@pytest.fixture(autouse=True)
def _restore_modules(monkeypatch):
    yield
    monkeypatch.undo()
    importlib.reload(sparql_resolver)
    importlib.reload(tsdb_joiner)
    importlib.reload(telemetry_simulator)


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


def test_simulator_write_endpoint_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("VM_WRITE_ENDPOINT", raising=False)
    importlib.reload(telemetry_simulator)
    assert telemetry_simulator.WRITE_ENDPOINT == "http://localhost:8428/write"


def test_simulator_write_endpoint_from_env(monkeypatch):
    monkeypatch.setenv("VM_WRITE_ENDPOINT", "http://victoriametrics:8428/write")
    importlib.reload(telemetry_simulator)
    assert telemetry_simulator.WRITE_ENDPOINT == "http://victoriametrics:8428/write"
