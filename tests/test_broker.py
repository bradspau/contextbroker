import ast
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import broker
import ingestion.telemetry_simulator as telemetry_simulator
from broker_engine import tsdb_joiner
from broker_engine.validation import ClientInputError

ROOT = Path(__file__).resolve().parent.parent

TS = 1_700_000_000.5
NOW = datetime.fromtimestamp(TS, timezone.utc)


def _resp(payload):
    r = Mock()
    r.json.return_value = payload
    r.raise_for_status = Mock()
    return r


def test_query_tsdb_metrics_returns_envelope_with_value_and_quality():
    value = _resp({"data": {"result": [{"value": [TS, "3"]}]}})
    sample_time = _resp({"data": {"result": [{"value": [TS, str(TS)]}]}})

    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[value, sample_time]) as mock_get:
        result = tsdb_joiner.query_tsdb_metrics(
            device="Router-A",
            interface="GigabitEthernet0/0/0",
            metric_type="input_drops",
            time_window="5m",
        )

    assert isinstance(result, dict)
    assert result["value"] == 3.0
    assert result["quality"]["missing"] is False
    query = mock_get.call_args_list[0].kwargs["params"]["query"]
    assert "interface_metrics_input_drops" in query
    assert 'device="Router-A"' in query
    assert 'interface="GigabitEthernet0/0/0"' in query
    assert "[5m]" in query


def test_query_tsdb_metrics_reports_missing_when_no_data():
    with patch("broker_engine.tsdb_joiner.requests.get",
               return_value=_resp({"data": {"result": []}})):
        result = tsdb_joiner.query_tsdb_metrics(
            device="Router-A",
            interface="GigabitEthernet0/0/0",
            metric_type="input_drops",
            time_window="5m",
        )

    assert result["value"] is None
    assert result["quality"]["missing"] is True


@pytest.mark.parametrize(
    "device,interface,metric_type,time_window",
    [
        ("Router-A; DROP", "GigabitEthernet0/0/0", "input_drops", "5m"),
        ("Router-A", "Gi0/0/0\"}", "input_drops", "5m"),
        ("Router-A", "GigabitEthernet0/0/0", "input_drops", "5m; rm -rf"),
    ],
)
def test_query_tsdb_metrics_rejects_unsafe_inputs(device, interface, metric_type, time_window):
    with pytest.raises(ClientInputError):
        tsdb_joiner.query_tsdb_metrics(
            device=device, interface=interface, metric_type=metric_type, time_window=time_window
        )


def test_bare_number_shim_is_gone_from_broker():
    assert not hasattr(broker, "query_tsdb_metrics")


def _non_test_python_files():
    skip = {"venv", "docs", ".git", "node_modules", "tests", ".planning", ".worktrees"}
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in skip or any(p in skip for p in rel.parts):
            continue
        yield path


def test_exactly_one_function_named_query_tsdb_metrics_outside_tests():
    found = []
    for path in _non_test_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == "query_tsdb_metrics":
                found.append((str(path.relative_to(ROOT)), node))
    assert [f[0] for f in found] == ["broker_engine/tsdb_joiner.py"]
    module = ast.parse((ROOT / "broker_engine/tsdb_joiner.py").read_text(encoding="utf-8"))
    assert [n.name for n in module.body if isinstance(n, ast.FunctionDef)].count("query_tsdb_metrics") == 1


def test_agent_tool_is_the_public_function():
    import agent_harness.tools as tools
    assert tools.query_tsdb_metrics is tsdb_joiner.query_tsdb_metrics


def test_measurement_name_constant_matches_telemetry_simulator():
    assert broker.MEASUREMENT_NAME == telemetry_simulator.MEASUREMENT_NAME == "interface_metrics"
