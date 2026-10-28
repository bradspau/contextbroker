import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import anthropic
import httpx2
import pytest

from agent_harness import tools, workflow
from agent_harness.workflow import run_agent
from broker_engine.assess import assess_intent  # noqa: F401
from data_contracts.envelope import build_observed


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
        "agent_harness.tools.query_intent_state_requirements",
        return_value={"metric": "input_drops", "operator": "==", "target_value": 0.0, "interfaces": []},
    ) as mock_tool:
        result = run_agent(
            "Is intent-core-health-01 compliant right now?", client=fake_client, model="test-model"
        )

    mock_tool.assert_called_once_with(intent_id="intent-core-health-01")
    assert result == "Compliant: 0 drops observed, target is 0."
    assert fake_client.messages.create.call_count == 2


def test_run_agent_returns_immediately_when_no_tool_call_needed():
    final_response = _response([_text_block("I need an intent id to check.")])
    fake_client = Mock()
    fake_client.messages.create.return_value = final_response

    result = run_agent("hello", client=fake_client, model="test-model")

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

    with patch("agent_harness.tools.query_intent_state_requirements") as mock_tool:
        result = run_agent(
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

    result = run_agent("check compliance", client=fake_client, model="test-model")

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

    with patch("agent_harness.tools.query_tsdb_metrics", return_value=None):
        result = run_agent("check compliance", client=fake_client, model="test-model")

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

    result = run_agent("do something", client=fake_client, model="test-model")

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
        "agent_harness.tools.query_intent_state_requirements",
        return_value={"metric": "input_drops", "operator": "==", "target_value": 0.0, "interfaces": []},
    ):
        result = run_agent("check compliance forever", client=fake_client, model="test-model")

    assert result == (
        f"Unable to reach a final answer after {workflow.MAX_ITERATIONS} tool-calling iterations."
    )
    assert fake_client.messages.create.call_count == workflow.MAX_ITERATIONS


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

    result = run_agent(
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
        run_agent(
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
        "agent_harness.tools.query_intent_state_requirements",
        return_value={"metric": "input_drops", "operator": "==", "target_value": 0.0, "interfaces": []},
    ) as mock_tool:
        result = run_agent(
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
        run_agent(
            "Is intent-core-health-01 compliant right now?",
            client=fake_claude_client,
            model="test-model",
        )




REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
INTENT = {
    "state": "intended", "source": "fuseki", "metric": "input_drops",
    "operator": "==", "target_value": 0.0,
    "interfaces": [
        {"device": "r1", "interface_name": "e0"},
        {"device": "r2", "interface_name": "e1"},
    ],
}


def test_harness_does_not_import_execution_layers():
    code = (
        "import sys, agent_harness.tools, agent_harness.workflow\n"
        "bad = [m for m in sys.modules if m.startswith(('governance_gate.controllers', 'action_lifecycle'))]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


def test_system_prompt_forbids_own_verdict():
    assert "do not compute compliance yourself" in tools.SYSTEM_PROMPT
    assert "assess_intent" in tools.SYSTEM_PROMPT
    assert "inconclusive" in tools.SYSTEM_PROMPT


def test_assess_intent_tool_registered_and_rejects_extra_args():
    names = {t["name"] for t in tools.TOOLS}
    assert "assess_intent" in names
    assert tools._TOOL_ALLOWED_ARGS["assess_intent"] == {"intent_id", "time_window", "source"}
    assert {t["function"]["name"] for t in tools.TOOLS_OPENAI} == names
    with patch("agent_harness.tools.assess_intent") as mock_tool:
        content, is_error = tools.execute_tool(
            "assess_intent",
            {"intent_id": "I1", "time_window": "5m", "query_endpoint": "http://evil"},
        )
    mock_tool.assert_not_called()
    assert is_error is True
    assert "query_endpoint" in json.loads(content)["error"]
    content, is_error = tools.execute_tool("delete_all_data", {})
    assert is_error is True


def test_assess_intent_tool_returns_deterministic_verdict():
    def join(device, interface, metric_type, time_window, **kw):
        value = 3.0 if device == "r1" else 0.0
        return build_observed(f"{device}:{interface}", value, "vm", NOW, NOW)

    with patch("broker_engine.assess.resolve_intent", return_value=INTENT), \
         patch("broker_engine.assess.join_observed_metric", side_effect=join):
        content, is_error = tools.execute_tool(
            "assess_intent", {"intent_id": "I1", "time_window": "5m"}
        )
    assert is_error is False
    body = json.loads(content)
    assert body["verdict"] == "failure"  # nonzero input_drops on r1
    assert {p["outcome"] for p in body["per_interface"]} == {"failure", "success"}
    assert set(body) >= {"verdict", "per_interface", "reasons", "context", "dropped_count"}


# ---- envelope-returning metric tool and sanitised errors ----

def _vm(payload):
    from unittest.mock import Mock
    r = Mock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


_METRIC_ARGS = {"device": "r1", "interface": "e0", "metric_type": "input_drops", "time_window": "30m"}


def test_metric_tool_returns_stale_envelope_to_model():
    value = _vm({"data": {"result": [{"metric": {}, "value": [1793009347, "0"]}]}})
    ts = _vm({"data": {"result": [{"metric": {}, "value": [1793009347, "1793008245"]}]}})
    from datetime import datetime, timezone
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[value, ts]), \
         patch("broker_engine.tsdb_joiner.datetime") as dt:
        dt.now.return_value = datetime.fromtimestamp(1793009347, timezone.utc)
        dt.fromtimestamp.side_effect = datetime.fromtimestamp
        content, is_error = tools.execute_tool("query_tsdb_metrics", dict(_METRIC_ARGS))
    assert is_error is False
    payload = json.loads(content)
    assert payload["value"] == 0.0
    assert payload["quality"]["stale"] is True
    assert payload["state"] == "observed" and payload["observed_at"]


def test_metric_tool_missing_envelope_reaches_model():
    with patch("broker_engine.tsdb_joiner.requests.get", return_value=_vm({"data": {"result": []}})):
        content, _ = tools.execute_tool("query_tsdb_metrics", dict(_METRIC_ARGS))
    payload = json.loads(content)
    assert payload["value"] is None and payload["quality"]["missing"] is True


def test_system_prompt_forbids_stale_as_current():
    assert "quality.stale" in tools.SYSTEM_PROMPT and "quality.missing" in tools.SYSTEM_PROMPT


def test_execute_tool_keeps_safe_messages():
    from broker_engine.validation import IntentNotFound
    with patch("agent_harness.tools.query_intent_state_requirements", side_effect=IntentNotFound("No such intent: 'x'")):
        content, is_error = tools.execute_tool("query_intent_state_requirements", {"intent_id": "x"})
    assert is_error and "No such intent" in json.loads(content)["error"]
    content, is_error = tools.execute_tool("query_tsdb_metrics", {**_METRIC_ARGS, "time_window": "5min"})
    assert is_error and "time_window" in json.loads(content)["error"]


def test_execute_tool_hides_upstream_details():
    import requests
    exc = requests.HTTPError("500 for url http://fuseki.internal:3030/ds/query?query=SELECT secret")
    with patch("agent_harness.tools.query_intent_state_requirements", side_effect=exc):
        content, is_error = tools.execute_tool("query_intent_state_requirements", {"intent_id": "x"})
    assert is_error
    assert json.loads(content) == {"error": "upstream data store unavailable"}
    assert "fuseki.internal" not in content and "secret" not in content


def test_execute_tool_generic_exception_has_type_only():
    with patch("agent_harness.tools.query_intent_state_requirements",
               side_effect=KeyError("http://vm.internal:8428/leak")):
        content, is_error = tools.execute_tool("query_intent_state_requirements", {"intent_id": "x"})
    assert is_error
    assert json.loads(content) == {"error": "tool error: KeyError"}
    assert "vm.internal" not in content


def test_assess_tool_source_enum_in_both_schemas():
    for schema in (
        next(t for t in tools.TOOLS if t["name"] == "assess_intent")["input_schema"],
        next(t for t in tools.TOOLS_OPENAI if t["function"]["name"] == "assess_intent")["function"]["parameters"],
    ):
        assert schema["properties"]["source"]["enum"] == ["graph", "tmf921"]
        assert "source" not in schema["required"]


def test_execute_tool_rejects_bad_source_and_undeclared_args():
    with patch("agent_harness.tools.assess_intent") as m:
        content, err = tools.execute_tool(
            "assess_intent", {"intent_id": "I1", "time_window": "5m", "source": "evil"})
        assert err and "source" in json.loads(content)["error"]
        for extra in ("query_endpoint", "base_url"):
            content, err = tools.execute_tool(
                "assess_intent", {"intent_id": "I1", "time_window": "5m", extra: "http://x"})
            assert err and "Unexpected argument" in json.loads(content)["error"]
        m.assert_not_called()


def test_execute_tool_routes_tmf921_source():
    with patch("agent_harness.tools.assess_intent", return_value={"verdict": "success"}) as m:
        content, err = tools.execute_tool(
            "assess_intent", {"intent_id": "I1", "time_window": "5m", "source": "tmf921"})
    assert not err
    m.assert_called_once_with(intent_id="I1", time_window="5m", source="tmf921")


def test_system_prompt_mentions_tmf921_rule():
    assert "source=tmf921" in tools.SYSTEM_PROMPT
    assert 'assess_intent(intent_id, time_window, source="graph"|"tmf921"' in tools.SYSTEM_PROMPT


# ---------- LLM_BACKEND switch ----------
@pytest.fixture(autouse=True)
def _default_backend(monkeypatch):
    # load_dotenv() never overrides a variable that is already set, so a
    # developer's .env cannot change which backend these tests exercise.
    monkeypatch.setenv("LLM_BACKEND", "claude")


def _lm_final(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))]
    )


def test_backend_lmstudio_never_contacts_claude(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "lmstudio")
    fake_claude = Mock()
    fake_lm = Mock()
    fake_lm.chat.completions.create.return_value = _lm_final("local answer")

    with patch.object(workflow, "_build_client") as build_claude:
        result = run_agent("q", client=fake_claude, model="m",
                           fallback_client=fake_lm, fallback_model="local-model")

    assert result == "local answer"
    fake_claude.messages.create.assert_not_called()
    build_claude.assert_not_called()
    assert fake_lm.chat.completions.create.call_args.kwargs["model"] == "local-model"


def test_backend_lmstudio_requires_model(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "lmstudio")
    monkeypatch.delenv("LMSTUDIO_MODEL", raising=False)
    with patch.object(workflow, "load_dotenv"), pytest.raises(RuntimeError, match="LMSTUDIO_MODEL"):
        run_agent("q", fallback_client=Mock())


def test_backend_lmstudio_reads_model_from_env(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "lmstudio")
    monkeypatch.setenv("LMSTUDIO_MODEL", "env-model")
    fake_lm = Mock()
    fake_lm.chat.completions.create.return_value = _lm_final("ok")
    run_agent("q", fallback_client=fake_lm)
    assert fake_lm.chat.completions.create.call_args.kwargs["model"] == "env-model"


@pytest.mark.parametrize("value", ["openai", "LM STUDIO", "lmstudio;claude"])
def test_backend_unknown_value_is_rejected(monkeypatch, value):
    monkeypatch.setenv("LLM_BACKEND", value)
    fake_claude = Mock()
    with pytest.raises(ValueError, match="LLM_BACKEND"):
        run_agent("q", client=fake_claude, model="m")
    fake_claude.messages.create.assert_not_called()


@pytest.mark.parametrize("value", ["", "  ", "Claude"])
def test_backend_blank_or_mixed_case_uses_claude(monkeypatch, value):
    monkeypatch.setenv("LLM_BACKEND", value)
    fake_claude = Mock()
    fake_claude.messages.create.return_value = _response([_text_block("claude answer")])
    assert run_agent("q", client=fake_claude, model="m") == "claude answer"


def test_time_window_format_is_declared_to_the_model():
    import re
    for tool in tools.TOOLS:
        window = tool["input_schema"]["properties"].get("time_window")
        if window is None:
            continue
        assert "5m" in window["description"]
        assert re.match(window["pattern"], "5m") and re.match(window["pattern"], "1h")
        assert not re.match(window["pattern"], "PT1H")
    assert "PT1H" in tools.SYSTEM_PROMPT and '"5m"' in tools.SYSTEM_PROMPT
