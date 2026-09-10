import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import anthropic
import httpx2
import pytest
import rdflib

import broker
import telemetry_simulator


ONTOLOGY_PATH = Path(__file__).resolve().parent.parent / "ontology" / "topology.ttl"


SPARQL_RESPONSE = {
    "results": {
        "bindings": [
            {
                "metric": {"value": "input_drops"},
                "operator": {"value": "=="},
                "targetValue": {"value": "0.0"},
                "device": {"value": "Router-A"},
                "interfaceName": {"value": "GigabitEthernet0/0/0"},
            },
            {
                "metric": {"value": "input_drops"},
                "operator": {"value": "=="},
                "targetValue": {"value": "0.0"},
                "device": {"value": "Router-B"},
                "interfaceName": {"value": "GigabitEthernet0/0/1"},
            },
        ]
    }
}

VM_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [{"metric": {}, "value": [1700000000, "3"]}],
    },
}

VM_EMPTY_RESPONSE = {"status": "success", "data": {"resultType": "vector", "result": []}}


def test_query_intent_state_requirements_parses_sparql_json():
    mock_response = Mock()
    mock_response.json.return_value = SPARQL_RESPONSE
    mock_response.raise_for_status = Mock()

    with patch("broker.requests.post", return_value=mock_response) as mock_post:
        result = broker.query_intent_state_requirements("intent-core-health-01")

    assert result["metric"] == "input_drops"
    assert result["operator"] == "=="
    assert result["target_value"] == 0.0
    assert result["interfaces"] == [
        {"device": "Router-A", "interface_name": "GigabitEthernet0/0/0"},
        {"device": "Router-B", "interface_name": "GigabitEthernet0/0/1"},
    ]
    _, kwargs = mock_post.call_args
    assert "intent-core-health-01" in kwargs["data"]["query"]


def test_query_intent_state_requirements_rejects_unsafe_intent_id():
    with pytest.raises(ValueError):
        broker.query_intent_state_requirements("intent-1 . } DROP ALL ; #")


def test_query_tsdb_metrics_parses_vector_result():
    mock_response = Mock()
    mock_response.json.return_value = VM_RESPONSE
    mock_response.raise_for_status = Mock()

    with patch("broker.requests.get", return_value=mock_response) as mock_get:
        result = broker.query_tsdb_metrics(
            device="Router-A",
            interface="GigabitEthernet0/0/0",
            metric_type="input_drops",
            time_window="5m",
        )

    assert result == 3.0
    _, kwargs = mock_get.call_args
    query = kwargs["params"]["query"]
    assert "interface_metrics_input_drops" in query
    assert 'device="Router-A"' in query
    assert 'interface="GigabitEthernet0/0/0"' in query
    assert "[5m]" in query


def test_query_tsdb_metrics_returns_none_when_no_data():
    mock_response = Mock()
    mock_response.json.return_value = VM_EMPTY_RESPONSE
    mock_response.raise_for_status = Mock()

    with patch("broker.requests.get", return_value=mock_response):
        result = broker.query_tsdb_metrics(
            device="Router-A",
            interface="GigabitEthernet0/0/0",
            metric_type="input_drops",
            time_window="5m",
        )

    assert result is None


@pytest.mark.parametrize(
    "device,interface,metric_type,time_window",
    [
        ("Router-A; DROP", "GigabitEthernet0/0/0", "input_drops", "5m"),
        ("Router-A", "Gi0/0/0\"}", "input_drops", "5m"),
        ("Router-A", "GigabitEthernet0/0/0", "input_drops", "5m; rm -rf"),
    ],
)
def test_query_tsdb_metrics_rejects_unsafe_inputs(device, interface, metric_type, time_window):
    with pytest.raises(ValueError):
        broker.query_tsdb_metrics(
            device=device, interface=interface, metric_type=metric_type, time_window=time_window
        )


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(block_id, name, input_):
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=input_)


def _response(content):
    return SimpleNamespace(content=content)


def test_run_agent_executes_tool_call_then_returns_final_message():
    first_response = _response(
        [_tool_use_block("call_1", "query_intent_state_requirements", {"intent_id": "intent-core-health-01"})]
    )
    final_response = _response(
        [_text_block("Compliant: 0 drops observed, target is 0.")]
    )

    fake_client = Mock()
    fake_client.messages.create.side_effect = [first_response, final_response]

    with patch(
        "broker.query_intent_state_requirements",
        return_value={"metric": "input_drops", "operator": "==", "target_value": 0.0, "interfaces": []},
    ) as mock_tool:
        result = broker.run_agent(
            "Is intent-core-health-01 compliant right now?", client=fake_client, model="test-model"
        )

    mock_tool.assert_called_once_with(intent_id="intent-core-health-01")
    assert result == "Compliant: 0 drops observed, target is 0."
    assert fake_client.messages.create.call_count == 2


def test_run_agent_returns_immediately_when_no_tool_call_needed():
    final_response = _response([_text_block("I need an intent id to check.")])
    fake_client = Mock()
    fake_client.messages.create.return_value = final_response

    result = broker.run_agent("hello", client=fake_client, model="test-model")

    assert result == "I need an intent id to check."
    assert fake_client.messages.create.call_count == 1


def test_run_agent_rejects_llm_supplied_extra_kwarg_and_recovers():
    """Finding 3 + 4: an LLM-supplied argument outside the tool's declared schema
    (e.g. an attacker-controlled query_endpoint, aka SSRF) must be rejected before
    dispatch, fed back to the model as a tool error, and NOT passed to the real
    tool function — and run_agent must recover rather than crash."""
    bad_block = _tool_use_block(
        "call_1",
        "query_intent_state_requirements",
        {"intent_id": "intent-core-health-01", "query_endpoint": "http://attacker.example/x"},
    )
    first_response = _response([bad_block])
    final_response = _response([_text_block("I hit an error reading the requirement.")])

    fake_client = Mock()
    fake_client.messages.create.side_effect = [first_response, final_response]

    with patch("broker.query_intent_state_requirements") as mock_tool:
        result = broker.run_agent(
            "Is intent-core-health-01 compliant right now?", client=fake_client, model="test-model"
        )

    mock_tool.assert_not_called()
    assert result == "I hit an error reading the requirement."
    assert fake_client.messages.create.call_count == 2

    # The conversation sent on the second call must contain a tool_result error message.
    second_call_messages = fake_client.messages.create.call_args_list[1].kwargs["messages"]
    tool_result_messages = [m for m in second_call_messages if m["role"] == "user" and isinstance(m["content"], list)]
    assert len(tool_result_messages) == 1
    tool_results = tool_result_messages[0]["content"]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_use_id"] == "call_1"
    assert tool_results[0]["is_error"] is True
    error_payload = json.loads(tool_results[0]["content"])
    assert "query_endpoint" in error_payload["error"]


def test_run_agent_recovers_from_unsafe_tool_argument_value():
    """Finding 4: an unsafe argument value (caught inside the tool function itself,
    e.g. an invalid time_window) must also be caught and fed back, not raised."""
    bad_block = _tool_use_block(
        "call_1",
        "query_tsdb_metrics",
        {
            "device": "Router-A",
            "interface": "GigabitEthernet0/0/0",
            "metric_type": "input_drops",
            "time_window": "5min",
        },
    )
    first_response = _response([bad_block])
    final_response = _response([_text_block("Let me retry with a valid window.")])

    fake_client = Mock()
    fake_client.messages.create.side_effect = [first_response, final_response]

    result = broker.run_agent("check compliance", client=fake_client, model="test-model")

    assert result == "Let me retry with a valid window."
    assert fake_client.messages.create.call_count == 2

    second_call_messages = fake_client.messages.create.call_args_list[1].kwargs["messages"]
    tool_result_messages = [m for m in second_call_messages if m["role"] == "user" and isinstance(m["content"], list)]
    assert len(tool_result_messages) == 1
    tool_results = tool_result_messages[0]["content"]
    assert tool_results[0]["is_error"] is True
    error_payload = json.loads(tool_results[0]["content"])
    assert "time_window" in error_payload["error"]


def test_run_agent_reports_no_data_explicitly_when_tsdb_returns_none():
    """A None result from query_tsdb_metrics (no data in window) must be surfaced
    to the model as an explicit {"status": "no_data"} tool result, not a bare
    null — so the model can't mistake missing telemetry for an observed zero."""
    block = _tool_use_block(
        "call_1",
        "query_tsdb_metrics",
        {
            "device": "Router-A",
            "interface": "GigabitEthernet0/0/0",
            "metric_type": "input_drops",
            "time_window": "5m",
        },
    )
    first_response = _response([block])
    final_response = _response([_text_block("Compliance is unknown due to missing telemetry.")])

    fake_client = Mock()
    fake_client.messages.create.side_effect = [first_response, final_response]

    with patch("broker.query_tsdb_metrics", return_value=None):
        result = broker.run_agent("check compliance", client=fake_client, model="test-model")

    assert result == "Compliance is unknown due to missing telemetry."

    second_call_messages = fake_client.messages.create.call_args_list[1].kwargs["messages"]
    tool_result_messages = [m for m in second_call_messages if m["role"] == "user" and isinstance(m["content"], list)]
    assert len(tool_result_messages) == 1
    tool_results = tool_result_messages[0]["content"]
    assert tool_results[0]["is_error"] is False
    payload = json.loads(tool_results[0]["content"])
    assert payload == {"status": "no_data"}


def test_run_agent_rejects_unknown_tool_name_without_raising():
    """An unknown/unregistered tool name must also be caught, not raised."""
    bad_block = _tool_use_block("call_1", "delete_all_data", {})
    first_response = _response([bad_block])
    final_response = _response([_text_block("I can't do that.")])

    fake_client = Mock()
    fake_client.messages.create.side_effect = [first_response, final_response]

    result = broker.run_agent("do something", client=fake_client, model="test-model")

    assert result == "I can't do that."


def test_run_agent_stops_after_max_iterations_without_raising():
    """Finding 4: if the model keeps returning tool calls forever, run_agent must
    give up after MAX_ITERATIONS create() calls instead of looping unboundedly."""
    always_tool_call_response = _response(
        [_tool_use_block("call_x", "query_intent_state_requirements", {"intent_id": "intent-core-health-01"})]
    )

    fake_client = Mock()
    fake_client.messages.create.return_value = always_tool_call_response

    with patch(
        "broker.query_intent_state_requirements",
        return_value={"metric": "input_drops", "operator": "==", "target_value": 0.0, "interfaces": []},
    ):
        result = broker.run_agent("check compliance forever", client=fake_client, model="test-model")

    assert result == (
        f"Unable to reach a final answer after {broker.MAX_ITERATIONS} tool-calling iterations."
    )
    assert fake_client.messages.create.call_count == broker.MAX_ITERATIONS


def _connection_error() -> anthropic.APIConnectionError:
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIConnectionError(request=req)


def _openai_tool_call(call_id, name, arguments_json):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments_json),
    )


def test_run_agent_falls_back_to_lmstudio_on_connection_error():
    """When Claude's API is unreachable, run_agent must fall back to the
    LM Studio client and still return an answer."""
    fake_claude_client = Mock()
    fake_claude_client.messages.create.side_effect = _connection_error()

    lmstudio_final = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="From LM Studio.", tool_calls=None))]
    )
    fake_lmstudio_client = Mock()
    fake_lmstudio_client.chat.completions.create.return_value = lmstudio_final

    result = broker.run_agent(
        "Is intent-core-health-01 compliant right now?",
        client=fake_claude_client,
        model="test-model",
        fallback_client=fake_lmstudio_client,
        fallback_model="local-model",
    )

    assert result == "From LM Studio."
    fake_claude_client.messages.create.assert_called_once()
    fake_lmstudio_client.chat.completions.create.assert_called_once()


def test_run_agent_does_not_fallback_on_non_connection_error():
    """Auth/rate-limit errors must propagate, not silently fall back — a
    misconfigured API key should fail loudly, not be masked by LM Studio."""
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(401, request=req)
    fake_claude_client = Mock()
    fake_claude_client.messages.create.side_effect = anthropic.AuthenticationError(
        "invalid x-api-key", response=resp, body=None
    )
    fake_lmstudio_client = Mock()

    with pytest.raises(anthropic.AuthenticationError):
        broker.run_agent(
            "Is intent-core-health-01 compliant right now?",
            client=fake_claude_client,
            model="test-model",
            fallback_client=fake_lmstudio_client,
            fallback_model="local-model",
        )

    fake_lmstudio_client.chat.completions.create.assert_not_called()


def test_run_agent_lmstudio_fallback_executes_tool_call():
    """The LM Studio fallback path must run the same tool-dispatch logic
    (allow-list, no_data handling) as the Claude path."""
    fake_claude_client = Mock()
    fake_claude_client.messages.create.side_effect = _connection_error()

    first_message = SimpleNamespace(
        content=None,
        tool_calls=[
            _openai_tool_call(
                "call_1", "query_intent_state_requirements", '{"intent_id": "intent-core-health-01"}'
            )
        ],
    )
    final_message = SimpleNamespace(content="Compliant via LM Studio.", tool_calls=None)
    fake_lmstudio_client = Mock()
    fake_lmstudio_client.chat.completions.create.side_effect = [
        SimpleNamespace(choices=[SimpleNamespace(message=first_message)]),
        SimpleNamespace(choices=[SimpleNamespace(message=final_message)]),
    ]

    with patch(
        "broker.query_intent_state_requirements",
        return_value={"metric": "input_drops", "operator": "==", "target_value": 0.0, "interfaces": []},
    ) as mock_tool:
        result = broker.run_agent(
            "Is intent-core-health-01 compliant right now?",
            client=fake_claude_client,
            model="test-model",
            fallback_client=fake_lmstudio_client,
            fallback_model="local-model",
        )

    mock_tool.assert_called_once_with(intent_id="intent-core-health-01")
    assert result == "Compliant via LM Studio."
    assert fake_lmstudio_client.chat.completions.create.call_count == 2


def test_run_agent_fallback_requires_lmstudio_model_env_var(monkeypatch):
    """If Claude is unreachable and LMSTUDIO_MODEL isn't configured, fail with
    a clear error instead of guessing a model name."""
    monkeypatch.delenv("LMSTUDIO_MODEL", raising=False)
    fake_claude_client = Mock()
    fake_claude_client.messages.create.side_effect = _connection_error()

    with pytest.raises(RuntimeError, match="LMSTUDIO_MODEL"):
        broker.run_agent(
            "Is intent-core-health-01 compliant right now?",
            client=fake_claude_client,
            model="test-model",
        )


def test_measurement_name_constant_matches_telemetry_simulator():
    """Finding 5: broker.py must import the shared constant, not hardcode its own copy."""
    assert broker.MEASUREMENT_NAME == telemetry_simulator.MEASUREMENT_NAME == "interface_metrics"


def test_intent_sparql_matches_real_ontology_content():
    """Finding 6: run the actual SPARQL text broker builds against the real ontology
    file (no HTTP mocking) so a predicate typo would fail this test, not just the
    mocked unit tests above."""
    graph = rdflib.Graph()
    graph.parse(ONTOLOGY_PATH, format="turtle")

    sparql = broker._build_intent_sparql("intent-core-health-01")
    rows = list(graph.query(sparql))

    assert len(rows) == 2
    seen = {(str(row.device), str(row.interfaceName)) for row in rows}
    assert seen == {
        ("Router-A", "GigabitEthernet0/0/0"),
        ("Router-B", "GigabitEthernet0/0/1"),
    }
    for row in rows:
        assert str(row.metric) == "input_drops"
        assert str(row.operator) == "=="
        assert str(row.targetValue) == "0.0"
