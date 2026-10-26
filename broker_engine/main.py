"""Broker REST + MCP interface over the resolver, joiner, pruner and evaluator."""

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict

from broker_engine.pruner import prune
from broker_engine.sparql_resolver import resolve_intent
from broker_engine.tsdb_joiner import join_observed_metric
from outcome_assurance.evaluate import evaluate_intent

DEFAULT_MAX_ENTITIES = 50
_NO_SUCH_INTENT = "No such intent"


def assess(intent_id: str, time_window: str, max_entities: int = DEFAULT_MAX_ENTITIES) -> dict:
    """Resolve the intent, join telemetry, evaluate on the full observations,
    then prune only the context returned."""
    intent = resolve_intent(intent_id)
    interfaces = intent["interfaces"]
    observations = {}
    for iface in interfaces:
        key = (iface["device"], iface["interface_name"])
        observations[key] = join_observed_metric(
            iface["device"], iface["interface_name"], intent["metric"], time_window
        )
    verdict = evaluate_intent(intent, observations)  # never depends on pruning
    pruned = prune(list(observations.values()), max(max_entities, len(interfaces)))
    return jsonable_encoder({
        "verdict": verdict.outcome.value,
        "per_interface": verdict.per_interface,
        "reasons": verdict.reasons,
        "context": [e.to_dict() for e in pruned.kept],
        "dropped_count": pruned.dropped_count,
    })


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
    def query_tsdb_metrics(device: str, interface: str, metric_type: str, time_window: str) -> float | None:
        """Read the observed value of a metric over a time window from the TSDB."""
        return join_observed_metric(device, interface, metric_type, time_window).value

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


@app.exception_handler(ValueError)
async def _value_error(request: Request, exc: ValueError):
    from fastapi.responses import JSONResponse

    status = 404 if str(exc).startswith(_NO_SUCH_INTENT) else 400
    return JSONResponse(status_code=status, content={"detail": str(exc)})


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
