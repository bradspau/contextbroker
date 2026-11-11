"""The MCP server is an optional extra: its nine pins live in requirements-mcp.txt, not in the
default install list, and the broker works (and its tests skip) without the package."""

import ast
import importlib.metadata as md
import os
import re
import subprocess
import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from broker_engine import main

ROOT = Path(__file__).resolve().parent.parent

MOVED = {
    "attrs": "26.1.0",
    "jsonschema": "4.26.0",
    "jsonschema-specifications": "2025.9.1",
    "mcp": "2.3.0",
    "mcp-types": "2.3.0",
    "python-multipart": "0.0.32",
    "referencing": "0.37.0",
    "rpds-py": "2026.9.1",
    "sse-starlette": "3.6.0",
}

MCP_TEST_IDS = [
    "tests/test_broker_api.py::test_mcp_tool_names",
    "tests/test_broker_api.py::test_mcp_metric_tool_returns_envelope",
    "tests/test_broker_api.py::test_default_source_is_tmf921",
    "tests/test_broker_api.py::test_mcp_assess_tool_has_no_source_argument",
    "tests/test_broker_api.py::test_mcp_neighbours_tool_accepts_as_of_and_known_at",
]

BLOCK_MCP = (
    "import sys\n"
    "for m in ('mcp', 'mcp.server', 'mcp.server.mcpserver'):\n"
    "    sys.modules[m] = None\n"
)


def _logical(path):
    """The file's requirement lines with backslash continuations joined and comments dropped."""
    out = []
    pending = ""
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        out.append((pending + line.strip()).strip())
        pending = ""
    return out


def _pins(path):
    """name -> version for every name==version requirement, hashed or not."""
    pins = {}
    for line in _logical(path):
        match = re.match(r"([A-Za-z0-9_.\-]+)==(\S+)(?: |$)", line)
        if match:
            pins[canonicalize_name(match.group(1))] = match.group(2)
    return pins


def _lines(path):
    out = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def test_the_default_lock_is_read_and_is_not_empty():
    assert len(_pins(ROOT / "requirements.txt")) > 50


def test_the_default_install_list_holds_none_of_the_nine_mcp_pins():
    default = _pins(ROOT / "requirements.txt")
    assert len(default) > 50
    assert set(default) & set(MOVED) == set()


def test_the_extra_is_the_default_lock_plus_exactly_the_nine_pins():
    default = _pins(ROOT / "requirements.txt")
    extra = _pins(ROOT / "requirements-mcp.txt")
    assert len(default) > 50
    for name, version in default.items():
        assert extra.get(name) == version, name
    for name, version in MOVED.items():
        assert extra.get(canonicalize_name(name)) == version, name
    assert set(extra) == set(default) | set(MOVED)


def test_the_extra_is_generated_from_its_inputs_with_the_default_lock_as_a_constraint():
    inputs = _lines(ROOT / "requirements-mcp.in")
    assert inputs[:3] == ["-c requirements.txt", "-r requirements.in", "mcp==2.3.0"]
    assert len(inputs) == 3
    assert all(line for line in _logical(ROOT / "requirements-mcp.txt"))
    for line in _logical(ROOT / "requirements-mcp.txt"):
        assert "--hash=sha256:" in line, line[:60]


def test_no_default_package_requires_one_of_the_nine():
    needs = []
    for name in _pins(ROOT / "requirements.txt"):
        try:
            requirements = md.requires(name) or []
        except md.PackageNotFoundError:
            continue  # not installed here, so its metadata cannot be read offline
        for text in requirements:
            req = Requirement(text)
            if req.marker is not None:
                if "extra" in str(req.marker):
                    continue
                if not req.marker.evaluate():
                    continue
            if canonicalize_name(req.name) in MOVED:
                needs.append((name, req.name))
    assert needs == []


def test_the_five_mcp_tests_skip_without_the_package():
    code = BLOCK_MCP + (
        "import pytest\n"
        f"sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider'] + {MCP_TEST_IDS!r}))\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=300
    )
    assert "5 skipped" in run.stdout, run.stdout + run.stderr
    assert run.returncode == 0, run.stdout + run.stderr


# --- the image and the module ---


def test_the_dockerfile_installs_the_default_list_only():
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    installs = [line for line in lines if "pip install" in line]
    assert len(installs) == 1 and "requirements.txt" in installs[0]
    assert not [line for line in lines if "requirements-mcp" in line]


def test_the_build_context_excludes_the_extra():
    ignored = _lines(ROOT / ".dockerignore")
    assert "requirements-mcp.txt" in ignored
    assert "requirements-mcp.in" in ignored


def test_importing_the_broker_module_loads_no_mcp_module(tmp_path):
    code = (
        "import sys, broker_engine.main; "
        "print(sorted(m for m in sys.modules if m == 'mcp' or m.startswith('mcp.')))"
    )
    env = dict(os.environ, BROKER_STATE_PATH=str(tmp_path / "state.sqlite"))
    run = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "[]"


def test_the_only_mcp_import_sits_inside_build_mcp_server():
    tree = ast.parse((ROOT / "broker_engine" / "main.py").read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mcp"):
            found.append(node)
        elif isinstance(node, ast.Import):
            found.extend(n for n in [node] if any(a.name.startswith("mcp") for a in node.names))
    assert len(found) == 1
    builder = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_mcp_server"
    )
    assert found[0] in list(ast.walk(builder))


def test_the_builder_without_the_package_names_the_extra_and_says_it_is_not_served(monkeypatch):
    for name in ("mcp", "mcp.server", "mcp.server.mcpserver"):
        monkeypatch.setitem(sys.modules, name, None)
    try:
        main.build_mcp_server()
    except RuntimeError as exc:
        assert "requirements-mcp.txt" in str(exc) and "not served" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("build_mcp_server did not raise")


def test_the_module_docstring_says_not_served_and_optional_extra():
    doc = main.__doc__
    assert "not served" in doc and "optional extra" in doc
    assert "built but not mounted" not in doc
