"""Model client interface with deterministic stubs.

Models are candidates: outputs are inferred evidence or routing hints, never
authorisation. Nothing here produces an action proposal.
"""
from datetime import datetime, timezone
from typing import Callable, Protocol

from artefact_governance.model_registry import ModelRegistry
from broker_engine.pruner import PrunedContext
from data_contracts.envelope import ContextEnvelope, Quality, StateCategory


class ModelNotApproved(PermissionError):
    """Raised when a model is not approved in the registry."""


class ModelClient(Protocol):
    name: str
    version: str

    def predict(self, inputs: list[ContextEnvelope]) -> ContextEnvelope: ...


def ensure_approved(registry: ModelRegistry, model) -> None:
    status = registry.status(model.name, model.version)
    if status != "approved":
        raise ModelNotApproved(
            f"model {model.name}@{model.version} is not approved (status: {status})"
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _usable(env: ContextEnvelope) -> bool:
    v = env.value
    return (
        not env.quality.missing
        and not env.quality.stale
        and isinstance(v, (int, float))
        and not isinstance(v, bool)
    )


class ThresholdLqmStub:
    """Flags (1.0) when any usable input exceeds the threshold, else 0.0."""

    def __init__(self, registry: ModelRegistry, name: str = "lqm-threshold-stub",
                 version: str = "0.1", threshold: float = 0.0,
                 now: Callable[[], datetime] = _utc_now):
        self.registry = registry
        self.name = name
        self.version = version
        self.threshold = threshold
        self._now = now

    def predict(self, inputs: list[ContextEnvelope]) -> ContextEnvelope:
        ensure_approved(self.registry, self)
        usable = [e for e in inputs if _usable(e)]
        times = [e.observed_at for e in inputs if e.observed_at is not None]
        provenance = {
            "model": self.name,
            "model_version": self.version,
            "input_window": {
                "entities": [e.entity_id for e in inputs],
                "from": min(times).isoformat() if times else None,
                "to": max(times).isoformat() if times else None,
            },
            "uncertainty": None,
        }
        if usable:
            value = 1.0 if any(e.value > self.threshold for e in usable) else 0.0
            quality = Quality()
        else:
            value = None
            quality = Quality(stale=False, missing=True)
        return ContextEnvelope(
            entity_id=inputs[0].entity_id if inputs else "unknown",
            state=StateCategory.INFERRED,
            value=value,
            source=self.name,
            observed_at=self._now(),
            quality=quality,
            provenance=provenance,
        )


class RouterStub:
    """System 1 router: a routing hint only ('investigate' or 'ignore')."""

    def __init__(self, registry: ModelRegistry, name: str = "system1-router-stub",
                 version: str = "0.1"):
        self.registry = registry
        self.name = name
        self.version = version

    def route(self, context: PrunedContext) -> str:
        ensure_approved(self.registry, self)
        for e in context.kept:
            q = e.quality
            if q.stale or q.missing or q.conflicts:
                return "investigate"
        return "ignore"
