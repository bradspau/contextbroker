"""Context envelope: the shared data contract for facts passed through the broker."""
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


@dataclass
class ContextEnvelope:
    entity_id: str
    state: StateCategory
    value: float | str | None
    source: str
    observed_at: datetime | None
    quality: Quality
    provenance: dict = field(default_factory=dict)

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
            "provenance": dict(self.provenance),
        }


def build_observed(entity_id, value, source, observed_at, now, max_age_s=DEFAULT_MAX_AGE_S) -> ContextEnvelope:
    missing = value is None
    stale = (
        not missing
        and observed_at is not None
        and (now - observed_at).total_seconds() > max_age_s
    )
    return ContextEnvelope(
        entity_id=entity_id,
        state=StateCategory.OBSERVED,
        value=value,
        source=source,
        observed_at=observed_at,
        quality=Quality(stale=stale, missing=missing),
    )
