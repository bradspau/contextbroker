"""Approved action catalogue. Only approved templates are ever returned."""
import re
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


_VERSION_RE = re.compile(r"[0-9]+(\.[0-9]+)*")


def _version_key(version: str) -> tuple[int, ...]:
    if type(version) is not str or not _VERSION_RE.fullmatch(version):
        raise ValueError(f"invalid version: {version!r}")
    return tuple(int(p) for p in version.split("."))


# The public name other packages import; the private name stays for this module's own use.
version_key = _version_key


class ActionCatalogue:
    def __init__(self, templates: Iterable[ActionTemplate] = ()):
        self._templates: list[ActionTemplate] = []
        for t in templates:
            self.add(t)

    def add(self, template: ActionTemplate) -> None:
        _version_key(template.version)  # reject malformed versions (ValueError)
        for t in self._templates:
            if t.template_id == template.template_id and _version_key(t.version) == _version_key(template.version):
                raise ValueError("duplicate template_id/version")
        self._templates.append(template)

    def get(self, template_id: str) -> ActionTemplate | None:
        approved = [t for t in self._templates
                    if t.template_id == template_id and t.approved is True]
        if not approved:
            return None
        return max(approved, key=lambda t: _version_key(t.version))
