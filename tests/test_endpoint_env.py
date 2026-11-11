import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from broker_engine import sparql_resolver, tsdb_joiner
from ingestion import telemetry_simulator

REPO_ROOT = Path(__file__).resolve().parent.parent


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


DEFAULT_DATA = "http://localhost:3030/network-dataset/data"

# The loader and the binder define exception classes that other test modules imported by name, so
# an in-process reload would split those classes in two. Read the constants in a fresh interpreter.
_PROBE = (
    "import inspect, ingestion.bind_intent as b, ingestion.load_ontologies as l;"
    "d = inspect.signature(b.bind).parameters['data_endpoint'].default;"
    "print(l.DEFAULT_DATA_ENDPOINT); print(b.DEFAULT_DATA_ENDPOINT); print(d);"
    "print(b.derive_update_endpoint(d))"
)


def _probe(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("FUSEKI_DATA_ENDPOINT", raising=False)
    else:
        monkeypatch.setenv("FUSEKI_DATA_ENDPOINT", value)
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=REPO_ROOT, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.split()


def test_data_endpoint_default_when_env_unset(monkeypatch):
    loader, binder, bind_default, update = _probe(monkeypatch, None)
    assert loader == binder == bind_default == DEFAULT_DATA
    assert update == "http://localhost:3030/network-dataset/update"


def test_data_endpoint_from_env_reaches_the_binder_and_its_update_endpoint(monkeypatch):
    loader, binder, bind_default, update = _probe(
        monkeypatch, "http://fuseki:3030/network-dataset/data")
    assert loader == binder == bind_default == "http://fuseki:3030/network-dataset/data"
    assert update == "http://fuseki:3030/network-dataset/update"


@pytest.mark.parametrize("module", ["ingestion.bind_intent", "ingestion.load_ontologies"])
def test_help_names_the_data_endpoint_variable(module):
    out = subprocess.run([sys.executable, "-m", module, "--help"], cwd=REPO_ROOT,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    assert "FUSEKI_DATA_ENDPOINT" in out.stdout


def test_env_example_has_the_two_write_endpoints_commented_once():
    lines = (REPO_ROOT / ".env.example").read_text().splitlines()
    for name, default in (("FUSEKI_DATA_ENDPOINT", DEFAULT_DATA),
                          ("VM_WRITE_ENDPOINT", "http://localhost:8428/write")):
        assert not any(line.startswith(name + "=") for line in lines), name
        entries = [i for i, line in enumerate(lines) if line.startswith(f"# {name}=")]
        assert len(entries) == 1, name
        assert lines[entries[0]] == f"# {name}={default}"
        assert lines[entries[0] - 1].startswith("# ") and "=" not in lines[entries[0] - 1][:20]
