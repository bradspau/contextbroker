"""Approved action catalogue. Only approved templates are ever returned."""
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ActionTemplate:
    template_id: str
    version: str
    scope_pattern: str  # fnmatch glob matched against the proposal scope by the gate
    preconditions: tuple[str, ...]
    expected_outcome: str
    recovery: str
    approved: bool


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


class ActionCatalogue:
    def __init__(self, templates: Iterable[ActionTemplate] = ()):
        self._templates: list[ActionTemplate] = []
        for t in templates:
            self.add(t)

    def add(self, template: ActionTemplate) -> None:
        _version_key(template.version)  # reject malformed versions (ValueError)
        self._templates.append(template)

    def get(self, template_id: str) -> ActionTemplate | None:
        approved = [t for t in self._templates
                    if t.template_id == template_id and t.approved is True]
        if not approved:
            return None
        return max(approved, key=lambda t: _version_key(t.version))
