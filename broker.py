"""Context broker: compares live TSDB telemetry against graph-declared intent."""
import json
import os
import re
import sys

import anthropic
import requests
from dotenv import load_dotenv
from openai import OpenAI

from telemetry_simulator import MEASUREMENT_NAME

FUSEKI_QUERY_ENDPOINT = "http://localhost:3030/network-dataset/query"
VM_QUERY_ENDPOINT = "http://localhost:8428/api/v1/query"
MAX_ITERATIONS = 10

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_\-/]+$")
_SAFE_WINDOW = re.compile(r"^[0-9]+[smhd]$")


def _validate_token(value: str, label: str) -> str:
    if not _SAFE_TOKEN.match(value):
        raise ValueError(f"Unsafe {label!r}: {value!r}")
    return value


def _build_intent_sparql(intent_id: str) -> str:
    _validate_token(intent_id, "intent_id")
    return f"""
    PREFIX : <http://example.org/network-ontology#>
    SELECT ?metric ?operator ?targetValue ?device ?interfaceName WHERE {{
      :{intent_id} :hasTargetMetric ?metric ;
                    :hasOperator ?operator ;
                    :hasTargetValue ?targetValue ;
                    :appliesToLink ?link .
      ?link :hasInterface ?iface .
      ?iface :onDevice ?device ;
             :interfaceName ?interfaceName .
    }}
    """


def query_intent_state_requirements(
    intent_id: str, query_endpoint: str = FUSEKI_QUERY_ENDPOINT
) -> dict:
    sparql = _build_intent_sparql(intent_id)

    response = requests.post(
        query_endpoint,
        data={"query": sparql},
        headers={"Accept": "application/sparql-results+json"},
    )
    response.raise_for_status()
    bindings = response.json()["results"]["bindings"]

    if not bindings:
        raise ValueError(f"No such intent: {intent_id!r}")

    first = bindings[0]
    return {
        "metric": first["metric"]["value"],
        "operator": first["operator"]["value"],
        "target_value": float(first["targetValue"]["value"]),
        "interfaces": [
            {
                "device": row["device"]["value"],
                "interface_name": row["interfaceName"]["value"],
            }
            for row in bindings
        ],
    }


def query_tsdb_metrics(
    device: str,
    interface: str,
    metric_type: str,
    time_window: str,
    query_endpoint: str = VM_QUERY_ENDPOINT,
) -> float | None:
    _validate_token(device, "device")
    _validate_token(interface, "interface")
    _validate_token(metric_type, "metric_type")
    if not _SAFE_WINDOW.match(time_window):
        raise ValueError(f"Unsafe time_window: {time_window!r}")

    promql = (
        f'max_over_time({MEASUREMENT_NAME}_{metric_type}'
        f'{{device="{device}", interface="{interface}"}}[{time_window}])'
    )

    response = requests.get(query_endpoint, params={"query": promql})
    response.raise_for_status()
    result = response.json()["data"]["result"]

    if not result:
        return None
    return float(result[0]["value"][1])


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
whether the intent is currently compliant, citing the numbers you compared.

If a query_tsdb_metrics result has "status": "no_data", no observed value exists for
that interface in the requested window — do not treat this as zero or as compliant.
Report compliance for that interface as unknown due to missing telemetry."""

TOOLS = [
    {
        "name": "query_intent_state_requirements",
        "description": "Read the declared requirement for an operational intent from the knowledge graph.",
        "input_schema": {
            "type": "object",
            "properties": {"intent_id": {"type": "string"}},
            "required": ["intent_id"],
        },
    },
    {
        "name": "query_tsdb_metrics",
        "description": "Read the observed value of a metric over a time window from the TSDB.",
        "input_schema": {
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
]

# Derive each tool's allowed argument keys from its own declared JSON-schema
# properties in TOOLS, so the allow-list can never drift from the schema.
_TOOL_ALLOWED_ARGS = {
    tool["name"]: set(tool["input_schema"]["properties"])
    for tool in TOOLS
}


def _to_openai_tool(tool: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }


# LM Studio speaks the OpenAI tool-calling wire format, so derive its tool list
# from the same TOOLS declarations Claude uses — one schema, no drift.
TOOLS_OPENAI = [_to_openai_tool(tool) for tool in TOOLS]


def _execute_tool(name: str, arguments: dict) -> tuple[str, bool]:
    """Run one tool call. Shared by both the Claude and LM Studio loops so the
    allow-list, SSRF guard, and no_data handling can't drift between them.
    Returns (json content string, is_error)."""
    try:
        # Restrict tool dispatch to the allow-list (derived from TOOLS,
        # so it can never drift from the declared schema)
        if name not in _TOOL_ALLOWED_ARGS:
            raise ValueError(f"Unknown tool: {name!r}")
        # Reject any argument key the tool's own schema doesn't declare
        # (prevents e.g. an LLM-supplied query_endpoint override / SSRF).
        allowed_keys = _TOOL_ALLOWED_ARGS[name]
        extra_keys = set(arguments) - allowed_keys
        if extra_keys:
            raise ValueError(f"Unexpected argument(s) for tool {name!r}: {sorted(extra_keys)}")
        # Look up the function dynamically from globals so patches work
        func = globals()[name]
        result = func(**arguments)
        # query_tsdb_metrics returns None for "no data in this window" —
        # surface that as an explicit status rather than a bare null, so
        # the model can't mistake missing telemetry for an observed zero.
        if result is None:
            result = {"status": "no_data"}
        return json.dumps(result), False
    except Exception as exc:
        return json.dumps({"error": str(exc)}), True


def _build_client() -> anthropic.Anthropic:
    load_dotenv()
    return anthropic.Anthropic()


def _build_lmstudio_client() -> OpenAI:
    load_dotenv()
    return OpenAI(
        base_url=os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1"),
        api_key="lm-studio",  # unused by LM Studio, but the OpenAI client requires a value
    )


def _run_claude(user_prompt: str, client: anthropic.Anthropic, model: str) -> str:
    messages = [{"role": "user", "content": user_prompt}]

    for _ in range(MAX_ITERATIONS):
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=TOOLS,
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
        if not tool_use_blocks:
            return "".join(block.text for block in response.content if block.type == "text")

        tool_results = []
        for block in tool_use_blocks:
            content, is_error = _execute_tool(block.name, block.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": tool_results})

    return f"Unable to reach a final answer after {MAX_ITERATIONS} tool-calling iterations."


def _run_lmstudio(user_prompt: str, client: OpenAI, model: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    for _ in range(MAX_ITERATIONS):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS_OPENAI
        )
        message = response.choices[0].message

        if not message.tool_calls:
            return message.content

        messages.append(
            {"role": "assistant", "content": message.content, "tool_calls": message.tool_calls}
        )
        for tool_call in message.tool_calls:
            try:
                arguments = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError as exc:
                content = json.dumps({"error": f"Invalid tool arguments JSON: {exc}"})
            else:
                content, _is_error = _execute_tool(tool_call.function.name, arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": content,
                }
            )

    return f"Unable to reach a final answer after {MAX_ITERATIONS} tool-calling iterations."


def run_agent(
    user_prompt: str,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
    fallback_client: OpenAI | None = None,
    fallback_model: str | None = None,
) -> str:
    if client is None:
        client = _build_client()
    if model is None:
        load_dotenv()
        model = os.environ.get("MODEL_NAME", "claude-opus-5")

    try:
        return _run_claude(user_prompt, client, model)
    except anthropic.APIConnectionError:
        # Claude's API is unreachable (network/DNS/timeout) — fall back to a
        # local LM Studio server. Auth and rate-limit errors are NOT caught
        # here, so a misconfigured API key still fails loudly.
        if fallback_client is None:
            fallback_client = _build_lmstudio_client()
        if fallback_model is None:
            load_dotenv()
            fallback_model = os.environ.get("LMSTUDIO_MODEL")
            if not fallback_model:
                raise RuntimeError(
                    "Claude API is unreachable and LMSTUDIO_MODEL is not set in .env — "
                    "cannot fall back to LM Studio."
                ) from None
        return _run_lmstudio(user_prompt, fallback_client, fallback_model)


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) or input("Ask the broker: ")
    print(run_agent(prompt))
