"""Agent tools, schemas and dispatch. Read-only consumer of broker_engine."""
import json

import requests

from broker_engine import operations
from broker_engine.assess import assess_intent
from broker_engine.sparql_resolver import resolve_intent
from broker_engine.tsdb_joiner import VM_QUERY_ENDPOINT, join_observed_metric
from broker_engine.validation import ClientInputError, IntentNotFound

query_intent_state_requirements = resolve_intent
query_metric_history = operations.metric_history
query_interfaces_with_nonzero = operations.interfaces_with_nonzero
query_interface_neighbours = operations.interface_neighbours


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


SYSTEM_PROMPT = """You are a network operations assistant. Many questions are about the state of the network and have nothing to do with an intent; use the tool that fits the question. You have these tools:

- assess_intent(intent_id, time_window, source="graph"|"tmf921", default graph): the tool for questions about a declared intent. source="graph" reads the requirement from the broker's own network graph (Fuseki); source="tmf921" asks the separate TMF921 intent server, whose intents and ontology are a different graph and a different intent model. The broker deterministically
  resolves the intent, reads telemetry for every interface, and evaluates compliance. It
  returns a verdict ("success", "failure" or "inconclusive"), per-interface results,
  reasons, supporting context and dropped_count.
- query_intent_state_requirements(intent_id): reads the declared requirement for an
  operational intent from the broker's network graph in Fuseki (drill-down only).
- query_tsdb_metrics(device, interface, metric_type, time_window): reads the observed
  value of a metric over a recent time window (drill-down only). It returns a context
  envelope: "value", "observed_at" and "quality" {"stale", "missing", "conflicts"}.

For a past question use assess_intent's as_of ("was it compliant 10 minutes ago" is as_of="10m", mode="at"; "was it ever non-compliant in the last 30 minutes" is time_window="30m", mode="within").
as_of and mode work for both sources. The requirement judged is always the intent's current definition (for source=tmf921 the bounds are read now from the TMF921 intent; nothing is pushed to the server for a past question). Say which moment the verdict describes (the evaluated_at field).

time_window is digits plus s, m, h or d (for example "5m" or "1h"), never ISO-8601 such as "PT1H".

To answer a compliance question, call assess_intent and then EXPLAIN the verdict it
returned, citing the per_interface results and reasons. Please do not compute compliance yourself: never compare observed values to targets on your own or override the verdict.
You may use the other two tools only to look up supporting detail.

- query_metric_history(device, interface, metric_type, time_window, as_of): min, max, last and sample count of one metric over a window, now or in the past.
- query_interfaces_with_nonzero(device, metric_type, time_window, as_of, limit): which interfaces of a device had a non-zero value in the window.
- query_interface_neighbours(device, interface): the interfaces at the other end of the declared topology links.

Use assess_intent only when asked whether a declared intent or requirement is met. For other questions ("what happened on...", "which interfaces had...", "what is connected to...") use the query tools and report what they return. These results carry no verdict; do not call anything compliant or non-compliant from them. In all of them quality.missing means there is no data (never zero or none), and quality.stale means the latest sample is old.

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

AS_OF = {
    "type": "string",
    "pattern": r"^[0-9]+[smhd]$",
    "description": 'Evaluate as of this long ago, e.g. "10m" for "10 minutes ago" (up to 30d). Omit for now. For assess_intent, works for both sources.',
}

TOOLS = [
    {
        "name": "query_intent_state_requirements",
        "description": "Read the declared requirement for an operational intent from the broker's network graph (Fuseki).",
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
        "name": "query_metric_history",
        "description": "Min, max, last value and sample count of one metric on one interface over a window, optionally as it stood in the past. No intent is needed and nothing is judged. quality.missing means no data, not zero.",
        "input_schema": {
            "type": "object",
            "properties": {
                "device": {"type": "string"},
                "interface": {"type": "string"},
                "metric_type": {"type": "string"},
                "time_window": TIME_WINDOW,
                "as_of": AS_OF,
            },
            "required": ["device", "interface", "metric_type", "time_window"],
        },
    },
    {
        "name": "query_interfaces_with_nonzero",
        "description": "Which interfaces of a device had a non-zero value of a metric (e.g. input_drops) in the window, worst first. quality.missing means the device reported no data, not that every interface is zero.",
        "input_schema": {
            "type": "object",
            "properties": {
                "device": {"type": "string"},
                "metric_type": {"type": "string"},
                "time_window": TIME_WINDOW,
                "as_of": AS_OF,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
            "required": ["device", "metric_type", "time_window"],
        },
    },
    {
        "name": "query_interface_neighbours",
        "description": "The interfaces at the other end of the declared topology links of an interface. Declared topology, not discovered state; quality.missing means the interface is not in the graph.",
        "input_schema": {
            "type": "object",
            "properties": {"device": {"type": "string"}, "interface": {"type": "string"}},
            "required": ["device", "interface"],
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
                "source": {
                    "type": "string", "enum": ["graph", "tmf921"], "default": "graph",
                    "description": "graph: the requirement in the broker's network graph (Fuseki). tmf921: the separate TMF921 intent server and its own graph.",
                },
                "as_of": AS_OF,
                "mode": {
                    "type": "string",
                    "enum": ["at", "within"],
                    "description": '"at": the latest sample at that moment (default when as_of is set). "within": was it EVER non-compliant during the time_window ending then.',
                },
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
