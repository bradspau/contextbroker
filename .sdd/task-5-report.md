# Task 5: LLM Orchestration Loop and CLI — Completion Report

## Summary

Successfully implemented the LLM orchestration loop and CLI for the context broker project. The implementation wires the two existing tool functions (`query_intent_state_requirements` and `query_tsdb_metrics`) into an OpenAI tool-calling loop, with a CLI interface for interactive use.

## What Was Done

### Step 1: Test-Driven Development

Added two new test functions to `tests/test_broker.py`:
- `test_run_agent_executes_tool_call_then_returns_final_message`: Verifies the agent executes tool calls in a loop, collects results, and returns the final message content
- `test_run_agent_returns_immediately_when_no_tool_call_needed`: Verifies the agent returns immediately when no tool calls are needed

Added import for `SimpleNamespace` from `types` module at the top of the test file to support mock construction.

### Step 2: Implementation in broker.py

**Updated imports** (top of file):
- Added `json` for parsing tool arguments
- Added `os` for environment variable access
- Added `sys` for CLI argument parsing
- Added `from dotenv import load_dotenv` for loading environment variables
- Added `from openai import OpenAI` for LLM client instantiation

**Added new module-level constants:**
- `SYSTEM_PROMPT`: Instructions for the agent to use the two query tools to answer network compliance questions
- `TOOLS`: OpenAI tool definitions for both `query_intent_state_requirements` and `query_tsdb_metrics`, following the OpenAI function calling schema

**Implemented `_build_client()` helper:**
- Loads environment variables from `.env` file
- Instantiates OpenAI client with `LLM_BASE_URL` and `LLM_API_KEY` from environment

**Implemented `run_agent()` function:**
- Signature: `run_agent(user_prompt: str, client: OpenAI | None = None, model: str | None = None) -> str`
- Default parameter handling: builds client and loads model from environment if not provided
- Implements agentic loop with message history
- Calls LLM with system prompt, user prompt, and tool definitions
- Handles tool calls by:
  - Parsing JSON arguments from tool call
  - Looking up function dynamically from module globals (enables test patching)
  - Executing function with unpacked arguments
  - Adding tool result to message history
- Returns final message content when no tool calls are present
- Key design: uses `globals()[tool_call.function.name]` to look up functions dynamically, ensuring that test patches via `@patch("broker.query_intent_state_requirements", ...)` properly intercept function calls

**Added CLI entry point:**
- `if __name__ == "__main__"` block accepts command-line arguments or prompts interactively
- Calls `run_agent()` with the user prompt and prints the result

## Commands Executed and Results

### Verify Tests Fail (Step 2)
```bash
pytest tests/test_broker.py -v -k run_agent
```
Result: ✅ 2 tests FAILED with `AttributeError: module 'broker' has no attribute 'run_agent'` (expected)

### Verify Tests Pass (Step 4)
```bash
pytest tests/test_broker.py -v
```
Result: ✅ 9 tests PASSED
- 7 existing tests (from Task 4)
- 2 new `run_agent` tests

### Run Full Test Suite (Step 5)
```bash
pytest tests/ -v
```
Result: ✅ 17 tests PASSED
- 9 broker tests (7 existing + 2 new)
- 1 docker-compose test (from Task 1)
- 3 seed_graph tests (from Task 2)
- 4 telemetry_simulator tests (from Task 3)

### Summary Output
```
============================== 17 passed in 1.79s ==============================
```

## Modified Files

### `/workspace/contextbroker/broker.py`
- Updated imports (lines 1-9)
- Added SYSTEM_PROMPT constant (lines 95-107)
- Added TOOLS constant (lines 109-139)
- Added _build_client() function (lines 141-143)
- Added run_agent() function (lines 146-179)
- Added CLI entry point (lines 182-184)

**Key implementation detail:** The `run_agent()` function uses `globals()[tool_call.function.name]` to dynamically look up tool functions at call time rather than storing references at module load time. This ensures that unit test patches using `@patch("broker.query_intent_state_requirements", ...)` properly intercept the function calls within the agentic loop.

### `/workspace/contextbroker/tests/test_broker.py`
- Added `from types import SimpleNamespace` import (line 1)
- Added `_tool_call()` helper function (lines 118-122)
- Added `test_run_agent_executes_tool_call_then_returns_final_message()` (lines 125-148)
- Added `test_run_agent_returns_immediately_when_no_tool_call_needed()` (lines 151-161)

## Manual Verification Step (Deferred)

The following step requires a real Docker stack, seeded data, and valid LLM credentials (not available in this sandbox):

```bash
docker compose up -d
source venv/bin/activate
python seed_graph.py
nohup python telemetry_simulator.py > telemetry.log 2>&1 &
sleep 10
python broker.py "Is intent-core-health-01 compliant right now?"
```

This manual test would:
1. Start Fuseki (knowledge graph) and VictoriaMetrics (TSDB) via docker-compose
2. Populate the knowledge graph with network topology and compliance intent
3. Start the telemetry simulator sending mock metrics
4. Invoke the CLI with a compliance question
5. Verify the agent calls both tools, compares metrics against thresholds, and returns a compliance assessment

This step should be performed on a machine with Docker installed and a real LLM API endpoint configured.

## Concerns

None. The implementation follows the brief exactly:
- ✅ Exact function signature matches brief: `run_agent(user_prompt: str, client=None, model: str | None = None) -> str`
- ✅ Exact imports and structure as specified
- ✅ Exact SYSTEM_PROMPT and TOOLS definitions as specified
- ✅ Test-first approach: wrote failing tests, then implemented
- ✅ All 9 broker tests pass (7 existing + 2 new)
- ✅ All 17 full suite tests pass
- ✅ No modifications to existing function signatures or behavior
- ✅ No use of git commands
- ✅ Existing venv reused without modifications
- ✅ tests/conftest.py left untouched

---

# Security Fix: Tool Dispatch Allow-List (Follow-up)

## Summary

Fixed a security vulnerability in the `run_agent()` function's tool dispatch mechanism where an unrestricted `globals()` lookup could call any function in the module's namespace if an LLM response (or injected tool call) specified its name.

## The Vulnerability

**Before (lines 171-172):**
```python
func = globals()[tool_call.function.name]
result = func(**arguments)
```

This code looked up **any** name in the module's global namespace without validation. If an LLM response or compromised tool call requested a function name matching an unintended module-level function (e.g. `load_dotenv`, `_validate_token`, `OpenAI`), the code would execute it with LLM-influenced arguments.

## The Fix

**After:**
1. Added module-level constant at line 16 (alongside other module constants):
   ```python
   _ALLOWED_TOOL_NAMES = {"query_intent_state_requirements", "query_tsdb_metrics"}
   ```

2. Updated dispatch logic in `run_agent()` (lines 171-174):
   ```python
   if tool_call.function.name not in _ALLOWED_TOOL_NAMES:
       raise ValueError(f"Unknown tool: {tool_call.function.name!r}")
   func = globals()[tool_call.function.name]
   result = func(**arguments)
   ```

The allow-list is still looked up via `globals()` at call time (not stored as a reference) so that `unittest.mock.patch("broker.query_intent_state_requirements", ...)` style test patching continues to work properly.

## Test Commands and Results

```bash
cd /workspace/contextbroker
venv/bin/pytest tests/test_broker.py -v
```

**Result: ✅ 9/9 PASSED**
```
tests/test_broker.py::test_query_intent_state_requirements_parses_sparql_json PASSED
tests/test_broker.py::test_query_intent_state_requirements_rejects_unsafe_intent_id PASSED
tests/test_broker.py::test_query_tsdb_metrics_parses_vector_result PASSED
tests/test_broker.py::test_query_tsdb_metrics_returns_none_when_no_data PASSED
tests/test_broker.py::test_query_tsdb_metrics_rejects_unsafe_inputs[Router-A; DROP-GigabitEthernet0/0/0-input_drops-5m] PASSED
tests/test_broker.py::test_query_tsdb_metrics_rejects_unsafe_inputs[Router-A-Gi0/0/0"}-input_drops-5m] PASSED
tests/test_broker.py::test_query_tsdb_metrics_rejects_unsafe_inputs[Router-A-GigabitEthernet0/0/0-input_drops-5m; rm -rf] PASSED
tests/test_broker.py::test_run_agent_executes_tool_call_then_returns_final_message PASSED
tests/test_broker.py::test_run_agent_returns_immediately_when_no_tool_call_needed PASSED
============================== 9 passed in 1.00s =======================================
```

```bash
venv/bin/pytest tests/ -v
```

**Result: ✅ 17/17 PASSED**
```
tests/test_broker.py::test_query_intent_state_requirements_parses_sparql_json PASSED
tests/test_broker.py::test_query_intent_state_requirements_rejects_unsafe_intent_id PASSED
tests/test_broker.py::test_query_tsdb_metrics_parses_vector_result PASSED
tests/test_broker.py::test_query_tsdb_metrics_returns_none_when_no_data PASSED
tests/test_broker.py::test_query_tsdb_metrics_rejects_unsafe_inputs[Router-A; DROP-GigabitEthernet0/0/0-input_drops-5m] PASSED
tests/test_broker.py::test_query_tsdb_metrics_rejects_unsafe_inputs[Router-A-Gi0/0/0"}-input_drops-5m] PASSED
tests/test_broker.py::test_query_tsdb_metrics_rejects_unsafe_inputs[Router-A-GigabitEthernet0/0/0-input_drops-5m; rm -rf] PASSED
tests/test_broker.py::test_run_agent_executes_tool_call_then_returns_final_message PASSED
tests/test_broker.py::test_run_agent_returns_immediately_when_no_tool_call_needed PASSED
tests/test_docker_compose.py::test_docker_compose_defines_victoriametrics_and_fuseki PASSED
tests/test_seed_graph.py::test_topology_ttl_parses_and_defines_intent_core_health_01 PASSED
tests/test_seed_graph.py::test_load_ttl_to_fuseki_posts_raw_turtle_bytes PASSED
tests/test_seed_graph.py::test_load_ttl_to_fuseki_raises_on_http_error PASSED
tests/test_telemetry_simulator.py::test_format_line_protocol_builds_expected_string PASSED
tests/test_telemetry_simulator.py::test_generate_metrics_returns_nonnegative_integers PASSED
tests/test_telemetry_simulator.py::test_build_payload_includes_both_routers PASSED
tests/test_telemetry_simulator.py::test_post_metrics_sends_payload_to_write_endpoint PASSED
============================== 17 passed in 1.46s =======================================
```

## Modified File

**`/workspace/contextbroker/broker.py`:**
- Line 16: Added `_ALLOWED_TOOL_NAMES = {"query_intent_state_requirements", "query_tsdb_metrics"}`
- Lines 171-174: Added allow-list check before dynamic function lookup in `run_agent()`

## Concerns

None. The allow-list is restrictive (exactly the two advertised tools), lookup still uses `globals()` at call time to preserve test patching semantics, and all existing tests continue to pass.
