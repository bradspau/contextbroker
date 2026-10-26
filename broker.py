"""Context broker CLI shim: the agent lives in agent_harness, data access in broker_engine."""
import sys

import requests  # noqa: F401  (tests patch broker.requests.*)

from agent_harness.tools import (  # noqa: F401
    SYSTEM_PROMPT,
    TOOLS,
    TOOLS_OPENAI,
    _TOOL_ALLOWED_ARGS,
    execute_tool,
    query_intent_state_requirements,
    query_tsdb_metrics,
)
from agent_harness.tools import execute_tool as _execute_tool  # noqa: F401
from agent_harness.workflow import (  # noqa: F401
    MAX_ITERATIONS,
    _build_client,
    _build_lmstudio_client,
    _run_claude,
    _run_lmstudio,
    run_agent,
)
from broker_engine.sparql_resolver import (  # noqa: F401
    FUSEKI_QUERY_ENDPOINT,
    _build_intent_sparql,
    resolve_intent,
)
from broker_engine.tsdb_joiner import VM_QUERY_ENDPOINT, join_observed_metric  # noqa: F401
from broker_engine.validation import _SAFE_TOKEN, _SAFE_WINDOW, _validate_token  # noqa: F401
from telemetry_simulator import MEASUREMENT_NAME  # noqa: F401

if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) or input("Ask the broker: ")
    print(run_agent(prompt))
