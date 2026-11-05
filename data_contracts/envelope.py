"""Context envelope: the shared data contract for facts passed through the broker.

Every envelope carries its provenance as a list of ProvenanceRecord."""
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

DEFAULT_MAX_AGE_S = 300


class StateCategory(str, Enum):
    INTENDED = "intended"
    CONFIGURED = "configured"
    OBSERVED = "observed"
    INFERRED = "inferred"


@dataclass
class Quality:
    stale: bool = False
    missing: bool = False
    conflicts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProvenanceRecord:
    """Where one piece of evidence came from, in four fields.

    source_system: the system that held it (for example "victoriametrics").
    reference: a record or query reference the broker built itself: a pinned
        metric or series name, a fluent IRI or a component name and version.
        Never text taken from an upstream answer.
    observed_at: when the evidence was observed, or None if that is unknown.
    retrieved_at: when the broker read it, or None.
    """
    source_system: str
    reference: str
    observed_at: datetime | None
    retrieved_at: datetime | None

    def to_dict(self) -> dict:
        return {
            "source_system": self.source_system,
            "reference": self.reference,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
        }


@dataclass
class ContextEnvelope:
    entity_id: str
    state: StateCategory
    value: float | str | None
    source: str
    observed_at: datetime | None
    quality: Quality
    provenance: list[ProvenanceRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "state": self.state.value,
            "value": self.value,
            "source": self.source,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "quality": {
                "stale": self.quality.stale,
                "missing": self.quality.missing,
                "conflicts": list(self.quality.conflicts),
            },
            "provenance": [record.to_dict() for record in self.provenance],
        }


def build_observed(entity_id, value, source, observed_at, now, max_age_s=DEFAULT_MAX_AGE_S) -> ContextEnvelope:
    """A non-finite sample (NaN or infinity) is no usable sample, so it is missing."""
    if isinstance(value, float) and not math.isfinite(value):
        value = None
    missing = value is None
    # Fail closed: a present value whose sample time is unknown is stale.
    stale = not missing and (
        observed_at is None or (now - observed_at).total_seconds() > max_age_s
    )
    return ContextEnvelope(
        entity_id=entity_id,
        state=StateCategory.OBSERVED,
        value=value,
        source=source,
        observed_at=observed_at,
        quality=Quality(stale=stale, missing=missing),
    )
