"""The logical context request and response contract, independent of its wire format (D-06).

This module is pure: no FastAPI, no I/O. It is the one tracked place for the closed sets a caller's
authority is expressed in (D-03): the purposes a request can name and the context classes it can ask
for. scripts/mint_token.py is standalone and keeps a copy of both tuples; a test pins the copy equal.
"""
from dataclasses import dataclass

PURPOSES = ("assurance", "diagnosis", "reporting")
CONTEXT_CLASSES = ("topology", "goals", "observations")

# The entity wildcard is this exact string. No glob matching of any kind (D-09).
ENTITY_WILDCARD = "*"


@dataclass(frozen=True)
class Authority:
    """What a caller may ask about: devices, purposes and context classes, from the signed token."""

    entities: frozenset[str]
    purposes: frozenset[str]
    classes: frozenset[str]

    def covers_entity(self, device: str) -> bool:
        """True when the wildcard is granted or `device` is named exactly."""
        return ENTITY_WILDCARD in self.entities or device in self.entities
