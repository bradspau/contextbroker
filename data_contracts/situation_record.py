"""What the broker keeps of an answered context request, and what a fetch by identity returns.

A stored situation is the resolved request parameters (the Phase 3 ``Situation``), the per-class
eligibility with its fixed reasons, the expiry and its basis, the evidence references (the
de-duplicated provenance records and the Context Graph node ids) and the verdict label. It holds no
metric value, no envelope and no fluent value: those are fetched again from their sources, so a
stored situation can never be read as the data itself (D-07).

When the events class was answered, the evidence also lists one provenance record per returned event.

It reuses the Phase 3 response models. The module is pure: no FastAPI and no I/O.
"""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from data_contracts.context_contract import (
    EXPIRY_CONFIGURED,
    EXPIRY_EVENT_WINDOW,
    EXPIRY_HISTORICAL,
    EXPIRY_NO_EVIDENCE,
    EXPIRY_OBSERVATION,
    ContextResponse,
    Eligibility,
    Provenance,
    Situation,
)

# `sit-` and 32 lower-case hex digits, as situation_id builds it. `\Z`, never `$`: a trailing newline
# must not match.
SITUATION_ID_PATTERN = re.compile(r"^sit-[0-9a-f]{32}\Z")

_VERDICTS = ("success", "failure", "inconclusive")


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Evidence(_Record):
    """References to the evidence a situation rested on, never the evidence itself."""

    provenance: list[Provenance]
    graph_nodes: list[str]


class StoredSituation(_Record):
    """The body of one situation row (D-07)."""

    situation: Situation
    eligibility: Eligibility
    expires_at: str
    expiry_basis: Literal[EXPIRY_HISTORICAL, EXPIRY_OBSERVATION, EXPIRY_EVENT_WINDOW, EXPIRY_CONFIGURED,
                          EXPIRY_NO_EVIDENCE]
    evidence: Evidence
    verdict: Literal[_VERDICTS] | None


class SituationRecord(StoredSituation):
    """A stored situation as a fetch returns it. `expired` is computed at read time from `expires_at`
    and is never stored: from that instant on the record is not current context."""

    expired: bool
    first_recorded_at: str
    last_recorded_at: str
    repeat_count: int


def _key(record: Provenance) -> tuple[str, str, str, str]:
    return (record.source_system, record.reference, record.observed_at or "", record.retrieved_at or "")


def stored_situation(response: ContextResponse) -> StoredSituation:
    """The record to keep for an answered request: references only, built purely from the response."""
    seen: dict[tuple[str, str, str, str], Provenance] = {}
    for envelope in response.envelopes:
        for record in envelope.provenance:
            seen.setdefault(_key(record), record)
    for value in response.fluent_values:
        seen.setdefault(_key(value.provenance), value.provenance)
    if response.events is not None:
        # A reference per returned event (its id), never the event: a fetched situation points at the
        # events it rested on and holds none of them (D-07).
        for event in response.events.events:
            seen.setdefault(_key(event.provenance), event.provenance)
    nodes = sorted(node.id for node in response.graph.nodes) if response.graph is not None else []
    label = response.verdict.get("verdict") if response.verdict is not None else None
    return StoredSituation(
        situation=response.situation, eligibility=response.eligibility,
        expires_at=response.expires_at, expiry_basis=response.expiry_basis,
        evidence=Evidence(provenance=[seen[key] for key in sorted(seen)], graph_nodes=nodes),
        verdict=label if label in _VERDICTS else None)
