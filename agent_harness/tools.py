"""Agent tools, schemas and dispatch. Read-only consumer of broker_engine."""
import json

import requests

from broker_engine.assess import assess_intent
from broker_engine.sparql_resolver import resolve_intent
from broker_engine.tsdb_joiner import VM_QUERY_ENDPOINT, join_observed_metric
from broker_engine.validation import ClientInputError, IntentNotFound

query_intent_state_requirements = resolve_intent


def query_tsdb_metrics(
    device: str,
    interface: str,
    metric_type: str,
    time_window: str,
    query_endpoint: str = VM_QUERY_ENDPOINT,
) -> dict:
    """Full context envelope (value plus quality flags), never a bare number."""
    return join_observed_metric(
        device, interface, metric_type, time_window, query_endpoint
    ).to_dict()


SYSTEM_PROMPT = """You are a network compliance agent. You have three tools:

- assess_intent(intent_id, time_window, source="graph"|"tmf921", default graph): the primary tool. The broker deterministically
  resolves the intent, reads telemetry for every interface, and evaluates compliance. It
  returns a verdict ("success", "failure" or "inconclusive"), per-interface results,
  reasons, supporting context and dropped_count.
- query_intent_state_requirements(intent_id): reads the declared requirement for an
  operational intent from the knowledge graph (drill-down only).
- query_tsdb_metrics(device, interface, metric_type, time_window): reads the observed
  value of a metric over a recent time window (drill-down only). It returns a context
  envelope: "value", "observed_at" and "quality" {"stale", "missing", "conflicts"}.

time_window is digits plus s, m, h or d (for example "5m" or "1h"), never ISO-8601 such as "PT1H".

To answer a compliance question, call assess_intent and then EXPLAIN the verdict it
returned, citing the per_interface results and reasons. Please do not compute compliance yourself: never compare observed values to targets on your own or override the verdict.
You may use the other two tools only to look up supporting detail.

A query_tsdb_metrics result with quality.stale true or quality.missing true (or a
null value) is NOT a current observation: never report it as current or compliant, and
never treat its value as an observed zero; report compliance as unknown.

If the verdict is "inconclusive", or any interface has missing telemetry ("no_data"),
never report the intent as compliant; report compliance as unknown due to missing or
unreliable telemetry.

With assess_intent source=tmf921 the verdict still comes from the broker's deterministic checks combined with the intent server's handling state; explain it and never compute or override it, and an "inconclusive" verdict means evidence was stale, missing, conflicted, drifted or unavailable and must never be presented as compliant."""

# The broker accepts only digits followed by s, m, h or d (e.g. "5m", "1h");
# ISO-8601 durations such as "PT1H" are rejected as an unsafe time_window.
TIME_WINDOW = {
    "type": "string",
    "pattern": r"^[0-9]+[smhd]$",
    "description": 'Look-back window: digits then s, m, h or d, e.g. "5m" or "1h". Not ISO-8601 ("PT1H" is rejected).',
}

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
        "description": "Read the observed metric as a context envelope (value, observed_at, quality.stale, quality.missing). A stale or missing result is not a current observation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "device": {"type": "string"},
                "interface": {"type": "string"},
                "metric_type": {"type": "string"},
                "time_window": TIME_WINDOW,
            },
            "required": ["device", "interface", "metric_type", "time_window"],
        },
    },
    {
        "name": "assess_intent",
        "description": "Deterministically assess an intent against observed telemetry; returns verdict, per-interface results, reasons and pruned context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent_id": {"type": "string"},
                "time_window": TIME_WINDOW,
                "source": {"type": "string", "enum": ["graph", "tmf921"], "default": "graph"},
            },
            "required": ["intent_id", "time_window"],
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


def execute_tool(name: str, arguments: dict) -> tuple[str, bool]:
    """Run one tool call. Shared by both the Claude and LM Studio loops so the
    allow-list, SSRF guard, and no_data handling can't drift between them.
    Returns (json content string, is_error)."""
    try:
        # Restrict tool dispatch to the allow-list (derived from TOOLS,
        # so it can never drift from the declared schema)
        if name not in _TOOL_ALLOWED_ARGS:
            raise ClientInputError(f"Unknown tool: {name!r}")
        # Reject any argument key the tool's own schema doesn't declare
        # (prevents e.g. an LLM-supplied query_endpoint override / SSRF).
        allowed_keys = _TOOL_ALLOWED_ARGS[name]
        extra_keys = set(arguments) - allowed_keys
        if extra_keys:
            raise ClientInputError(f"Unexpected argument(s) for tool {name!r}: {sorted(extra_keys)}")
        # Look up the function dynamically from globals so patches work
        for key, prop in next(t for t in TOOLS if t["name"] == name)["input_schema"]["properties"].items():
            if "enum" in prop and key in arguments and arguments[key] not in prop["enum"]:
                raise ClientInputError(f"Invalid value for {key!r}: must be one of {prop['enum']}")
        func = globals()[name]
        result = func(**arguments)
        # query_tsdb_metrics returns None for "no data in this window" —
        # surface that as an explicit status rather than a bare null, so
        # the model can't mistake missing telemetry for an observed zero.
        if result is None:
            result = {"status": "no_data"}
        return json.dumps(result), False
    except (ClientInputError, IntentNotFound) as exc:
        # Our own validation/not-found messages: safe to show the model.
        return json.dumps({"error": str(exc)}), True
    except requests.RequestException:
        # Never leak internal hostnames/URLs/queries to the model provider.
        return json.dumps({"error": "upstream data store unavailable"}), True
    except Exception as exc:
        return json.dumps({"error": f"tool error: {type(exc).__name__}"}), True
