"""Broker REST + MCP interface over the resolver, joiner, pruner and evaluator."""

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from broker_engine.assess import DEFAULT_MAX_ENTITIES, assess_intent
from broker_engine.pruner import prune  # noqa: F401
from broker_engine.validation import ClientInputError, IntentNotFound
from broker_engine.sparql_resolver import resolve_intent
from broker_engine.tsdb_joiner import join_observed_metric
from outcome_assurance.evaluate import evaluate_intent  # noqa: F401

def assess(intent_id: str, time_window: str, max_entities: int = DEFAULT_MAX_ENTITIES) -> dict:
    """Thin wrapper over assess_intent using this module's resolver/joiner."""
    return assess_intent(
        intent_id, time_window, max_entities,
        resolve=resolve_intent, join=join_observed_metric,
    )


def _metric(device: str, interface: str, metric_type: str, time_window: str) -> dict:
    return join_observed_metric(device, interface, metric_type, time_window).to_dict()


def build_mcp_server():
    """MCP server exposing the same tool names as the LLM tools plus assess_intent."""
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("The 'mcp' package (>=2) is required for the MCP server") from exc

    server = MCPServer("context-broker")

    @server.tool()
    def query_intent_state_requirements(intent_id: str) -> dict:
        """Read the declared requirement for an operational intent from the knowledge graph."""
        return resolve_intent(intent_id)

    @server.tool()
    def query_tsdb_metrics(device: str, interface: str, metric_type: str, time_window: str) -> dict:
        """Read the observed metric as a context envelope incl. quality flags (stale/missing)."""
        return _metric(device, interface, metric_type, time_window)

    @server.tool()
    def assess_intent(intent_id: str, time_window: str) -> dict:
        """Assess an intent against observed telemetry and return verdict plus pruned context."""
        return assess(intent_id, time_window)

    return server


app = FastAPI(title="Context Broker")


def _allow_only(*names: str):
    allowed = set(names)

    def check(request: Request) -> None:
        extra = sorted(set(request.query_params) - allowed)
        if extra:
            raise HTTPException(422, f"Undeclared query parameter(s): {extra}")

    return Depends(check)


@app.exception_handler(ClientInputError)
async def _client_error(request: Request, exc: ClientInputError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(IntentNotFound)
async def _not_found(request: Request, exc: IntentNotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(requests.RequestException)
async def _upstream_error(request: Request, exc: requests.RequestException):
    return JSONResponse(status_code=502, content={"detail": "upstream data store unavailable"})


class AssessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: str
    time_window: str


@app.get("/context/intent/{intent_id}", dependencies=[_allow_only()])
def get_intent(intent_id: str) -> dict:
    return resolve_intent(intent_id)


@app.get("/context/metric", dependencies=[_allow_only("device", "interface", "metric_type", "time_window")])
def get_metric(device: str, interface: str, metric_type: str, time_window: str) -> dict:
    return _metric(device, interface, metric_type, time_window)


@app.post("/context/assess")
def post_assess(body: AssessRequest, request: Request) -> dict:
    if request.query_params:
        raise HTTPException(422, "Undeclared query parameter(s)")
    return assess(body.intent_id, body.time_window)
