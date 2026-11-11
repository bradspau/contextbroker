"""Context broker CLI shim: the agent lives in agent_harness, data access in broker_engine."""
import sys

import requests  # noqa: F401  (tests patch broker.requests.*)

from agent_harness.tools import (  # noqa: F401
    SYSTEM_PROMPT,
    TOOLS,
    TOOLS_OPENAI,
    execute_tool,
)
from agent_harness.workflow import (  # noqa: F401
    MAX_ITERATIONS,
    run_agent,
)
from broker_engine.sparql_resolver import FUSEKI_QUERY_ENDPOINT  # noqa: F401
from broker_engine.tsdb_joiner import VM_QUERY_ENDPOINT, join_observed_metric  # noqa: F401
from broker_engine.validation import SAFE_TOKEN, SAFE_WINDOW, validate_token  # noqa: F401
from data_contracts.telemetry import MEASUREMENT_NAME  # noqa: F401


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) or input("Ask the broker: ")
    result = run_agent(prompt)
    print(result.answer)
    print(f"backend: {result.backend}", file=sys.stderr)

