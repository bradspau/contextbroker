"""Broker REST interface (MCP server built but not mounted) over the joiner, pruner, operations queries and the TMF921 assess."""

from typing import Literal

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from broker_engine.assess import assess_intent
from broker_engine import operations
from broker_engine.pruner import prune  # noqa: F401
from broker_engine.validation import ClientInputError
from broker_engine.tsdb_joiner import join_observed_metric
from outcome_assurance.evaluate import evaluate_intent  # noqa: F401

def assess(intent_id: str, time_window: str, source: str = "tmf921",
           as_of: str | None = None, mode: str | None = None) -> dict:
    """Thin wrapper over assess_intent using this module's joiner."""
    return assess_intent(
        intent_id, time_window,
        join=join_observed_metric, source=source, as_of=as_of, mode=mode,
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
    def query_tsdb_metrics(device: str, interface: str, metric_type: str, time_window: str) -> dict:
        """Read the observed metric as a context envelope incl. quality flags (stale/missing)."""
        return _metric(device, interface, metric_type, time_window)

    @server.tool()
    def assess_intent(
        intent_id: str, time_window: str,
        as_of: str | None = None, mode: Literal["at", "within"] | None = None,
    ) -> dict:
        """Assess a TMF921 intent against observed telemetry and return verdict plus pruned context.
        as_of ("10m") assesses as of that long ago; mode "within" checks the whole window.
        The goals are read now from the TMF921 intent; a past question pushes nothing to the intent server."""
        return assess(intent_id, time_window, as_of=as_of, mode=mode)

    @server.tool()
    def query_metric_history(device: str, interface: str, metric_type: str, time_window: str,
                             as_of: str | None = None) -> dict:
        """Min, max, last and sample count of a metric over a window (no intent needed)."""
        return operations.metric_history(device, interface, metric_type, time_window, as_of)

    @server.tool()
    def query_interfaces_with_nonzero(device: str, metric_type: str, time_window: str,
                                      as_of: str | None = None, limit: int = 50) -> dict:
        """Interfaces of a device whose metric was non-zero in the window."""
        return operations.interfaces_with_nonzero(device, metric_type, time_window, as_of, limit)

    @server.tool()
    def query_interface_neighbours(device: str, interface: str) -> dict:
        """Interfaces on the other end of the topology links of this interface."""
        return operations.interface_neighbours(device, interface)

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


@app.exception_handler(requests.RequestException)
async def _upstream_error(request: Request, exc: requests.RequestException):
    return JSONResponse(status_code=502, content={"detail": "upstream data store unavailable"})


class AssessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: str
    time_window: str
    source: Literal["tmf921"] = "tmf921"
    as_of: str | None = None
    mode: Literal["at", "within"] | None = None


@app.get("/context/metric", dependencies=[_allow_only("device", "interface", "metric_type", "time_window")])
def get_metric(device: str, interface: str, metric_type: str, time_window: str) -> dict:
    return _metric(device, interface, metric_type, time_window)


@app.post("/context/assess")
def post_assess(body: AssessRequest, request: Request) -> dict:
    if request.query_params:
        raise HTTPException(422, "Undeclared query parameter(s)")
    return assess(body.intent_id, body.time_window, source=body.source,
                  as_of=body.as_of, mode=body.mode)


@app.get("/context/metric-history",
         dependencies=[_allow_only("device", "interface", "metric_type", "time_window", "as_of")])
def get_metric_history(device: str, interface: str, metric_type: str, time_window: str,
                       as_of: str | None = None) -> dict:
    return operations.metric_history(device, interface, metric_type, time_window, as_of)


@app.get("/context/interfaces-nonzero",
         dependencies=[_allow_only("device", "metric_type", "time_window", "as_of", "limit")])
def get_interfaces_nonzero(device: str, metric_type: str, time_window: str,
                           as_of: str | None = None, limit: int = operations.DEFAULT_INTERFACES) -> dict:
    return operations.interfaces_with_nonzero(device, metric_type, time_window, as_of, limit)


@app.get("/context/neighbours", dependencies=[_allow_only("device", "interface")])
def get_neighbours(device: str, interface: str) -> dict:
    return operations.interface_neighbours(device, interface)
