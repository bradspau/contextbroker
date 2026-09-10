### Task 5: LLM Orchestration Loop and CLI

**Files:**
- Modify: `broker.py` (add imports at the top, append the rest to the end)
- Modify: `tests/test_broker.py` (append new tests)

**Interfaces:**
- Consumes: `query_intent_state_requirements` and `query_tsdb_metrics` from `broker.py` (already implemented in Task 4 — do not change their signatures or behavior).
- Produces: `run_agent(user_prompt: str, client=None, model: str | None = None) -> str` — the `client` and `model` parameters exist solely so tests can inject a fake client instead of hitting a real LLM endpoint. Use this exact signature.

**Global constraints for this task:**
- **Do not use git.** No `git add`/`git commit` — just edit the local files.
- Env vars (exact names): `LLM_API_KEY`, `LLM_BASE_URL`, `MODEL_NAME`, already present with placeholder values in `.env` from Task 1. Load them with `python-dotenv`'s `load_dotenv()`.
- `openai` and `python-dotenv` are already installed in `contextbroker/venv/` and already in `requirements.txt` from Task 1 — no new dependency needed.
- venv already exists at `contextbroker/venv/` — reuse it (`source venv/bin/activate` or run `venv/bin/pytest` directly). `tests/conftest.py` already exists from Task 4 and makes `import broker` work regardless of invocation directory — do not remove or duplicate it.

- [ ] **Step 1: Write failing tests for the tool-calling loop**

Append to the existing `tests/test_broker.py` (add these at the end of the file, alongside its existing imports of `Mock`/`patch`/`pytest`/`broker` — you'll need one new import, `from types import SimpleNamespace`, added at the top of the file with the other imports):

```python
from types import SimpleNamespace


def _tool_call(call_id, name, arguments_json):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments_json),
    )


def test_run_agent_executes_tool_call_then_returns_final_message():
    first_message = SimpleNamespace(
        content=None,
        tool_calls=[_tool_call("call_1", "query_intent_state_requirements", '{"intent_id": "intent-core-health-01"}')],
    )
    final_message = SimpleNamespace(content="Compliant: 0 drops observed, target is 0.", tool_calls=None)

    fake_client = Mock()
    fake_client.chat.completions.create.side_effect = [
        SimpleNamespace(choices=[SimpleNamespace(message=first_message)]),
        SimpleNamespace(choices=[SimpleNamespace(message=final_message)]),
    ]

    with patch(
        "broker.query_intent_state_requirements",
        return_value={"metric": "input_drops", "operator": "==", "target_value": 0.0, "interfaces": []},
    ) as mock_tool:
        result = broker.run_agent(
            "Is intent-core-health-01 compliant right now?", client=fake_client, model="test-model"
        )

    mock_tool.assert_called_once_with(intent_id="intent-core-health-01")
    assert result == "Compliant: 0 drops observed, target is 0."
    assert fake_client.chat.completions.create.call_count == 2


def test_run_agent_returns_immediately_when_no_tool_call_needed():
    final_message = SimpleNamespace(content="I need an intent id to check.", tool_calls=None)
    fake_client = Mock()
    fake_client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=final_message)]
    )

    result = broker.run_agent("hello", client=fake_client, model="test-model")

    assert result == "I need an intent id to check."
    assert fake_client.chat.completions.create.call_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_broker.py -v -k run_agent
```

Expected: FAIL with `AttributeError: module 'broker' has no attribute 'run_agent'`.

- [ ] **Step 3: Add the new imports at the top of broker.py, then append the orchestration loop and CLI to the end**

At the top of `broker.py`, replace the existing two-line import block:
```python
import re

import requests
```
with:
```python
import json
import os
import re
import sys

import requests
from dotenv import load_dotenv
from openai import OpenAI
```

Then add the rest to the end of `broker.py` (after the existing `query_tsdb_metrics` function):

```python
SYSTEM_PROMPT = """You are a network compliance agent. You have two tools:

- query_intent_state_requirements(intent_id): reads the declared requirement for an
  operational intent from the knowledge graph (target metric, operator, target value,
  and the interfaces it applies to).
- query_tsdb_metrics(device, interface, metric_type, time_window): reads the observed
  value of a metric over a recent time window from the telemetry database.

To answer a compliance question: first call query_intent_state_requirements to learn
what is expected and which interfaces to check. Then call query_tsdb_metrics for each
of those interfaces to learn what is actually happening. Mathematically compare the
observed value against the target value using the given operator, and state plainly
whether the intent is currently compliant, citing the numbers you compared."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_intent_state_requirements",
            "description": "Read the declared requirement for an operational intent from the knowledge graph.",
            "parameters": {
                "type": "object",
                "properties": {"intent_id": {"type": "string"}},
                "required": ["intent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_tsdb_metrics",
            "description": "Read the observed value of a metric over a time window from the TSDB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "interface": {"type": "string"},
                    "metric_type": {"type": "string"},
                    "time_window": {"type": "string"},
                },
                "required": ["device", "interface", "metric_type", "time_window"],
            },
        },
    },
]

_TOOL_IMPLEMENTATIONS = {
    "query_intent_state_requirements": query_intent_state_requirements,
    "query_tsdb_metrics": query_tsdb_metrics,
}


def _build_client() -> OpenAI:
    load_dotenv()
    return OpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"])


def run_agent(user_prompt: str, client: OpenAI | None = None, model: str | None = None) -> str:
    if client is None:
        client = _build_client()
    if model is None:
        load_dotenv()
        model = os.environ["MODEL_NAME"]

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    while True:
        response = client.chat.completions.create(model=model, messages=messages, tools=TOOLS)
        message = response.choices[0].message

        if not message.tool_calls:
            return message.content

        messages.append(
            {"role": "assistant", "content": message.content, "tool_calls": message.tool_calls}
        )
        for tool_call in message.tool_calls:
            arguments = json.loads(tool_call.function.arguments)
            result = _TOOL_IMPLEMENTATIONS[tool_call.function.name](**arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result),
                }
            )


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) or input("Ask the broker: ")
    print(run_agent(prompt))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_broker.py -v
```

Expected: PASS (9 tests — the 7 from Task 4 plus the 2 new `run_agent` tests).

- [ ] **Step 5: Run the full test suite**

```bash
pytest tests/ -v
```

Expected: all tests across all four test files PASS (17 total: 1 docker-compose + 3 seed_graph + 4 telemetry_simulator + 9 broker).

- [ ] **Step 6: Note the manual-Docker-and-LLM-verification step in your report**

This step needs a real Docker stack, seeded data, and a real LLM_API_KEY/LLM_BASE_URL/MODEL_NAME (not available in this sandbox) — do not attempt it, just note in your report that it's deferred:

```bash
docker compose up -d
source venv/bin/activate
python seed_graph.py
nohup python telemetry_simulator.py > telemetry.log 2>&1 &
sleep 10
python broker.py "Is intent-core-health-01 compliant right now?"
```

Do NOT run a "Commit" step — this project is not using git. Skip any git commands entirely.
