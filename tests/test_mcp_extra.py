"""The MCP server is an optional extra: its nine pins live in requirements-mcp.txt, not in the
default install list, and the broker works (and its tests skip) without the package."""

import importlib.metadata as md
import re
import subprocess
import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

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


def _pins(path):
    """name -> version for every name==version line; comments and blanks are skipped."""
    pins = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        match = re.fullmatch(r"([A-Za-z0-9_.\-]+)==(\S+)", line)
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


def test_the_default_install_list_holds_none_of_the_nine_mcp_pins():
    assert set(_pins(ROOT / "requirements.txt")) & set(MOVED) == set()


def test_the_extra_includes_the_default_list_first_and_holds_exactly_the_nine_pins():
    lines = _lines(ROOT / "requirements-mcp.txt")
    assert lines[0] == "-r requirements.txt"
    assert len(lines) == 1 + len(MOVED)
    assert _pins(ROOT / "requirements-mcp.txt") == MOVED


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
