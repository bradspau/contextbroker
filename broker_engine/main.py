"""Broker REST interface (MCP server built but not mounted) over the joiner, pruner, operations queries and the TMF921 assess.

REST callers present a bearer token (broker_engine/auth.py); every /context/* route is refused
with 401 without one. The in-process MCP server and agent harness call broker functions directly
and present none (D-08); a served MCP server must authenticate."""

import logging
from contextlib import asynccontextmanager
from typing import Literal

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from broker_engine.assess import assess_intent
from broker_engine.auth import SCOPE_ASSESS, SCOPE_READ, need, need_authority_claims, require_token
from broker_engine.faults import log_fault
from broker_engine.health import probe_upstreams
from broker_engine import context_request, operations
from broker_engine.pruner import prune  # noqa: F401
from broker_engine.validation import ClientInputError, max_as_of_seconds
from broker_state.process import load_settings, open_process_store, reset_store
from broker_state.store import STATE_UNAVAILABLE, StateUnavailable
from broker_engine.tsdb_joiner import join_observed_metric
from data_contracts.context_contract import ContextRequest, ContextResponse
from data_contracts.situation_record import SituationRecord
from outcome_assurance.evaluate import evaluate_intent  # noqa: F401

log = logging.getLogger(__name__)

# A bad BROKER_RETENTION_PERIOD stops start-up here instead of failing every temporal request.
max_as_of_seconds()


def assess(intent_id: str, time_window: str, source: str = "tmf921",
           as_of: str | None = None, mode: str | None = None,
           known_at: str | None = None) -> dict:
    """Thin wrapper over assess_intent using this module's joiner."""
    return assess_intent(
        intent_id, time_window,
        join=join_observed_metric, source=source, as_of=as_of, mode=mode,
        known_at=known_at,
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
        known_at: str | None = None,
    ) -> dict:
        """Assess a TMF921 intent against observed telemetry and return verdict plus pruned context.
        as_of ("10m") assesses as of that long ago; mode "within" checks the whole window;
        known_at ("1h") uses what the broker had recorded that long ago.
        A past question uses the goal in force then, from the network graph (named in goals),
        and pushes nothing to the intent server."""
        return assess(intent_id, time_window, as_of=as_of, mode=mode, known_at=known_at)

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
    def query_interface_neighbours(device: str, interface: str, as_of: str | None = None,
                                   known_at: str | None = None) -> dict:
        """Interfaces on the other end of the topology links of this interface.

        as_of ("10m") returns the links valid that long ago (now if omitted); known_at ("1h")
        answers from what the broker had recorded that long ago (the latest if omitted)."""
        return operations.interface_neighbours(device, interface, as_of=as_of, known_at=known_at)

    return server


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Open the state store before serving and close it on shutdown (D-04).

    A malformed state variable raises ValueError (it names the variable, never the value) and an
    unusable file raises StateUnavailable; either stops the start-up, so the API never serves without
    its state and never falls back to memory. The open is one blocking call before any request."""
    load_settings()
    open_process_store()
    try:
        yield
    finally:
        reset_store()


app = FastAPI(title="Context Broker", lifespan=_lifespan)
app.middleware("http")(require_token)


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
    log_fault(log, exc, "upstream data store unavailable")
    return JSONResponse(status_code=502, content={"detail": "upstream data store unavailable"})


@app.exception_handler(context_request.Refused)
async def _refused(request: Request, exc: context_request.Refused):
    # The message is always one of the fixed details of the contract, never caller or server text.
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(StateUnavailable)
async def _state_unavailable(request: Request, exc: StateUnavailable):
    # The store has already logged the exception class. The body is fixed text, never the cause.
    return JSONResponse(status_code=503, content={"detail": STATE_UNAVAILABLE})


@app.get("/health", dependencies=[_allow_only()])
def get_health() -> JSONResponse:
    """Public: whether each upstream answers, as fixed strings. Outside /context/, so no token."""
    body = probe_upstreams()
    return JSONResponse(status_code=200 if body["status"] == "ok" else 503, content=body)


class AssessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: str
    time_window: str
    source: Literal["tmf921"] = "tmf921"
    as_of: str | None = None
    mode: Literal["at", "within"] | None = None
    known_at: str | None = None


@app.get("/context/metric",
         dependencies=[need(SCOPE_READ), _allow_only("device", "interface", "metric_type", "time_window")])
def get_metric(device: str, interface: str, metric_type: str, time_window: str) -> dict:
    return _metric(device, interface, metric_type, time_window)


@app.post("/context/assess", dependencies=[need(SCOPE_ASSESS)])
def post_assess(body: AssessRequest, request: Request) -> dict:
    if request.query_params:
        raise HTTPException(422, "Undeclared query parameter(s)")
    return assess(body.intent_id, body.time_window, source=body.source,
                  as_of=body.as_of, mode=body.mode, known_at=body.known_at)


@app.post("/context/request", response_model=ContextResponse,
          dependencies=[need(SCOPE_READ), need_authority_claims()])
def post_context_request(body: ContextRequest, request: Request) -> ContextResponse:
    """Ask for context: why (purpose), about what (a device, optionally an interface), when (now, or
    as_of and known_at offsets) and which classes (topology, goals, observations, events).

    The token needs the context:read scope and the authority claims (entities, purposes, classes). The
    requester is the token subject, never a field of the body. Naming an intent_id also needs
    context:assess. The answer carries a situation identity, an eligibility result per requested class,
    an expiry with its basis, fluent values and envelopes with provenance records, and the Context
    Graph with the limits that cut it. The events class takes an optional window_seconds and reads stored
    syslog events from the state file; only for a recognised link event does it also read the device's
    link fluents from Fuseki, to show a conflict. It never returns the raw line or message text.
    Eligibility describes the fitness of the evidence and grants no execution authority. A verdict is
    present only for an intent with the goals and observations
    classes; it is kept apart from eligibility and the two can disagree."""
    if request.query_params:
        raise HTTPException(422, "Undeclared query parameter(s)")
    return context_request.handle(body, request.state.caller)


@app.get("/context/situation/{situation_id}", response_model=SituationRecord,
         dependencies=[need(SCOPE_READ), need_authority_claims(), _allow_only()])
def get_situation(situation_id: str, request: Request) -> SituationRecord:
    """Fetch a recorded situation for the caller who asked for it: the request parameters, the evidence
    references, the eligibility and the expiry. It needs context:read and authority claims covering the
    stored scope, and context:assess when the situation names an intent. `expired` is true from
    `expires_at` on and the record is never current after that. An unknown, malformed or someone else's
    id is the same 404. It reads only the state store and never refreshes the record."""
    found = context_request.fetch_situation(situation_id, request.state.caller)
    if found is None:
        raise HTTPException(404, context_request.SITUATION_NOT_FOUND)
    return found


@app.get("/context/metric-history",
         dependencies=[need(SCOPE_READ),
                       _allow_only("device", "interface", "metric_type", "time_window", "as_of")])
def get_metric_history(device: str, interface: str, metric_type: str, time_window: str,
                       as_of: str | None = None) -> dict:
    return operations.metric_history(device, interface, metric_type, time_window, as_of)


@app.get("/context/interfaces-nonzero",
         dependencies=[need(SCOPE_READ),
                       _allow_only("device", "metric_type", "time_window", "as_of", "limit")])
def get_interfaces_nonzero(device: str, metric_type: str, time_window: str,
                           as_of: str | None = None, limit: int = operations.DEFAULT_INTERFACES) -> dict:
    return operations.interfaces_with_nonzero(device, metric_type, time_window, as_of, limit)


@app.get("/context/neighbours",
         dependencies=[need(SCOPE_READ), _allow_only("device", "interface", "as_of", "known_at")])
def get_neighbours(device: str, interface: str, as_of: str | None = None,
                   known_at: str | None = None) -> dict:
    return operations.interface_neighbours(device, interface, as_of=as_of, known_at=known_at)
