"""No module imports another repository module's underscore name (HYG-02, D-81).

An AST walk over every non-test .py file reports each `from <repository module> import _name`
as (file, module, name). The found set must equal PENDING exactly, and PENDING is empty: any private
import between modules fails the test.
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

# Empty since plan 08-12: no module imports another repository module's underscore name. Each entry would be (file, module, name).
PENDING = set()


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


def test_an_entry_in_pending_that_is_not_an_offence_makes_the_comparison_fail():
    grown = set(PENDING) | {("broker.py", "broker_engine.validation", "_x")}
    assert found_offences() != grown


def test_the_scan_covers_the_broker_packages_and_the_top_level_files():
    names = {p.relative_to(ROOT).as_posix() for p in scanned_files()}
    assert {"broker.py", "seed_graph.py", "broker_engine/validation.py"} <= names
    assert not any(n.startswith(("tests/", "venv/")) for n in names)


@pytest.mark.parametrize("old", ["_SAFE_TOKEN", "_SAFE_WINDOW", "_UNIT_SECONDS", "_SAFE_RETENTION",
                                 "_validate_token"])
def test_the_old_validation_names_are_gone_not_aliased(old):
    from broker_engine import validation
    assert not hasattr(validation, old)
    assert hasattr(validation, old.lstrip("_"))


def test_the_token_and_intent_id_checks_are_public_not_aliased():
    import data_contracts.metric_map as metric_map
    import intent_management.tmf921_client as client
    assert hasattr(metric_map, "check_token") and not hasattr(metric_map, "_check_token")
    assert hasattr(client, "check_intent_id") and not hasattr(client, "_check_intent_id")


def test_the_short_name_helper_is_public_not_aliased():
    import outcome_assurance.tmf921_report as report
    assert hasattr(report, "short_name") and not hasattr(report, "_short_name")


def test_the_pending_set_is_empty():
    assert PENDING == set()
