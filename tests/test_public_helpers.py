"""No module imports another repository module's underscore name (HYG-02, D-81).

An AST walk over every non-test .py file reports each `from <repository module> import _name`
as (file, module, name). The found set must equal PENDING exactly, so PENDING can only shrink
by being edited: a new offence fails the test, and so does fixing one without removing its entry.
Tests may use private names of the module they test, so tests/ is not scanned.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# Directories that are not the repository's own source: environments, untracked docs, tests
# (which may reach into the module they test) and the proxy, which is its own image.
SKIPPED_DIRS = {"venv", ".venv", "docs", "tests", ".git", "lodlive-proxy", "__pycache__",
                "node_modules", ".planning", ".sdd", ".beads"}

# The offences that stay until plan 08-12 renames them. Each entry is (file, module, name).
PENDING = {
    ("broker.py", "agent_harness.tools", "_TOOL_ALLOWED_ARGS"),
    ("broker.py", "agent_harness.workflow", "_build_client"),
    ("broker.py", "agent_harness.workflow", "_build_lmstudio_client"),
    ("broker.py", "agent_harness.workflow", "_run_claude"),
    ("broker.py", "agent_harness.workflow", "_run_lmstudio"),
    ("broker.py", "broker_engine.validation", "_SAFE_TOKEN"),
    ("broker.py", "broker_engine.validation", "_validate_token"),
    ("broker_engine/auth.py", "broker_engine.validation", "_SAFE_TOKEN"),
    ("data_contracts/metric_map.py", "broker_engine.validation", "_SAFE_TOKEN"),
    ("broker_engine/operations.py", "broker_engine.validation", "_validate_token"),
    ("broker_engine/tsdb_joiner.py", "broker_engine.validation", "_validate_token"),
    ("broker_engine/binding_resolver.py", "data_contracts.metric_map", "_check_token"),
    ("broker_engine/context_graph.py", "data_contracts.metric_map", "_check_token"),
    ("ingestion/bind_intent.py", "data_contracts.metric_map", "_check_token"),
    ("broker_engine/binding_resolver.py", "intent_management.tmf921_client", "_check_intent_id"),
    ("ingestion/bind_intent.py", "intent_management.tmf921_client", "_check_intent_id"),
    ("intent_management/tmf921_source.py", "intent_management.tmf921_client", "_check_intent_id"),
    ("broker_engine/context_eligibility.py", "outcome_assurance.tmf921_report", "_short_name"),
    ("broker_engine/context_request.py", "outcome_assurance.tmf921_report", "_short_name"),
    ("ingestion/bind_intent.py", "outcome_assurance.tmf921_report", "_short_name"),
    ("intent_management/tmf921_source.py", "outcome_assurance.tmf921_report", "_short_name"),
}


def repository_modules(root=ROOT):
    """Top-level packages (a directory holding __init__.py) plus broker and seed_graph."""
    packages = {p.parent.name for p in root.glob("*/__init__.py") if p.parent.name not in SKIPPED_DIRS}
    return packages | {"broker", "seed_graph"}


def scanned_files(root=ROOT):
    return sorted(p for p in root.rglob("*.py")
                  if not SKIPPED_DIRS.intersection(p.relative_to(root).parts))


def private_imports(source, filename, modules):
    """Every `from <repository module> import _name` in `source` as (file, module, name)."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        if node.module.split(".")[0] not in modules:
            continue
        found |= {(filename, node.module, a.name) for a in node.names if a.name.startswith("_")}
    return found


def found_offences(root=ROOT):
    modules = repository_modules(root)
    found = set()
    for path in scanned_files(root):
        found |= private_imports(path.read_text(), path.relative_to(root).as_posix(), modules)
    return found


def test_the_private_imports_found_are_exactly_the_pending_set():
    assert found_offences() == PENDING


def test_the_rule_reports_a_private_import_with_the_file_name():
    source = "from broker_engine.validation import _x, y\nimport os\nfrom os import _z\n"
    assert private_imports(source, "demo.py", {"broker_engine"}) == {
        ("demo.py", "broker_engine.validation", "_x")}


def test_the_rule_ignores_relative_imports_and_other_packages():
    source = "from . import _x\nfrom .a import _y\nfrom requests import _z\n"
    assert private_imports(source, "demo.py", {"broker_engine"}) == set()


def test_dropping_an_entry_from_pending_makes_the_comparison_fail():
    shrunk = set(PENDING)
    shrunk.discard(next(iter(sorted(PENDING))))
    assert found_offences() != shrunk


def test_the_scan_covers_the_broker_packages_and_the_top_level_files():
    names = {p.relative_to(ROOT).as_posix() for p in scanned_files()}
    assert {"broker.py", "seed_graph.py", "broker_engine/validation.py"} <= names
    assert not any(n.startswith(("tests/", "venv/")) for n in names)


@pytest.mark.parametrize("old", ["_SAFE_WINDOW", "_UNIT_SECONDS", "_SAFE_RETENTION"])
def test_the_old_validation_names_are_gone_not_aliased(old):
    from broker_engine import validation
    assert not hasattr(validation, old)
    assert hasattr(validation, old.lstrip("_"))
