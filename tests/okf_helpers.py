"""Builders for knowledge trees in tmp_path: bundle files, raw bytes and the action catalogue file.

The files are written as text with LF line endings and a quoted version, the way the tracked
`knowledge/` directory is authored. `write_raw` writes exact bytes so a refusal test can break one rule.
"""
import json
from pathlib import Path

# One valid action template with all seven ActionTemplate fields.
TEMPLATE = {
    "template_id": "example-collect-interface-counters",
    "version": "1.0",
    "scope_pattern": "Router-*",
    "preconditions": ["interface exists in the topology"],
    "expected_outcome": "interface counters are read and recorded",
    "recovery": "none needed; the action only reads",
    "approved": True,
}


def _flow(items) -> str:
    return json.dumps(list(items))


def bundle_text(identifier: str, version: str = "1.0", *, status: str = "draft",
                title: str = "Interface error handling", entities=("Router-*",),
                purposes=("diagnosis",), ontology_classes=None,
                source_references=("repo:design/event-store.md",), linked_actions=(),
                body: str = "Check the counters on the interface.\n") -> str:
    lines = [
        "---",
        f"identifier: {identifier}",
        f"version: {json.dumps(version)}",
        f"status: {status}",
        f"title: {json.dumps(title)}",
        "scope:",
        f"  entities: {_flow(entities)}",
        f"  purposes: {_flow(purposes)}",
    ]
    if ontology_classes is not None:
        lines.append(f"  ontology_classes: {_flow(ontology_classes)}")
    lines.append(f"source_references: {_flow(source_references)}")
    if linked_actions:
        lines.append(f"linked_actions: {_flow(linked_actions)}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def write_bundle(root: Path, identifier: str, version: str = "1.0", **kwargs) -> Path:
    """Write root/bundles/<identifier>/<version>.md and return its path."""
    path = Path(root) / "bundles" / identifier / f"{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bundle_text(identifier, version, **kwargs).encode("utf-8"))
    return path


def write_raw(root: Path, identifier: str, version: str, data: bytes) -> Path:
    """Write exact bytes as root/bundles/<identifier>/<version>.md."""
    path = Path(root) / "bundles" / identifier / f"{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_catalogue(root: Path, templates) -> Path:
    """Write root/action-catalogue.yaml with the given template dicts (JSON is valid YAML)."""
    path = Path(root) / "action-catalogue.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not templates:
        path.write_bytes(b"templates: []\n")
        return path
    text = "templates:\n" + "".join(
        "  -\n" + "".join(f"    {key}: {json.dumps(value)}\n" for key, value in t.items())
        for t in templates)
    path.write_bytes(text.encode("utf-8"))
    return path
