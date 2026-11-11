"""Pins between the tracked Phase 8 note and the code (plan 08-13).

A note that disagrees with the code misleads whoever operates or integrates against it, so the parts
that must match are read from the note and compared with the code: the two settings and their
ranges, the printed series-limit query form and conflict text, the proxy's accepted forms and refused
words, the upstream names, the cache and public names, and the commands. The wording rules of
CLAUDE.md and the licensed-marker rule are checked over the note too.
"""
import importlib.util
import os
import re
from pathlib import Path

import pytest

from broker_engine import operations, upstream_http, validation
from tests.test_design_notes import (
    FAILS_CLOSED, FORBIDDEN_MARKERS, FORBIDDEN_WORDS, _setting_rows, _text,
)

ROOT = Path(__file__).resolve().parent.parent
NOTE = ROOT / "design" / "performance-and-hygiene.md"
PROXY_PATH = ROOT / "lodlive-proxy" / "jsonp_proxy.py"


def _section(text: str, heading: str) -> str:
    """The body of one `## heading` section, up to the next `## ` heading."""
    start = text.index("\n## " + heading + "\n")
    rest = text[start + 1:]
    nxt = rest.find("\n## ", 3)
    return rest if nxt == -1 else rest[:nxt]


def _limits_cells(text: str) -> dict[str, str]:
    """variable -> the third cell (limits) of each BROKER setting row."""
    cells = {}
    for line in text.splitlines():
        parts = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(parts) >= 3 and re.fullmatch(r"`BROKER_[A-Z_]+`", parts[0]):
            cells[parts[0].strip("`")] = parts[2]
    return cells


@pytest.fixture(scope="module")
def proxy():
    spec = importlib.util.spec_from_file_location("jsonp_proxy_for_note", PROXY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- the settings -----------------------------------------------------------------------------


def test_the_note_lists_both_settings_with_the_default_the_code_gives():
    rows = _setting_rows(_text(NOTE))
    assert rows.get("BROKER_TELEMETRY_WORKERS") == str(validation.telemetry_workers({}))
    assert rows.get("BROKER_TSDB_SERIES_LIMIT") == str(validation.tsdb_series_limit({}))
    assert (rows["BROKER_TELEMETRY_WORKERS"], rows["BROKER_TSDB_SERIES_LIMIT"]) == ("8", "500")


def test_the_limits_cell_states_the_range_the_code_enforces():
    cells = _limits_cells(_text(NOTE))
    assert f"1 to {validation.MAX_TELEMETRY_WORKERS}" in cells["BROKER_TELEMETRY_WORKERS"]
    assert f"1 to {validation.MAX_TSDB_SERIES_LIMIT}" in cells["BROKER_TSDB_SERIES_LIMIT"]
    # the ranges are the ones the parsers enforce
    validation.telemetry_workers({validation.TELEMETRY_WORKERS_ENV: str(validation.MAX_TELEMETRY_WORKERS)})
    with pytest.raises(ValueError):
        validation.telemetry_workers({validation.TELEMETRY_WORKERS_ENV: str(validation.MAX_TELEMETRY_WORKERS + 1)})
    with pytest.raises(ValueError):
        validation.tsdb_series_limit({validation.TSDB_SERIES_LIMIT_ENV: str(validation.MAX_TSDB_SERIES_LIMIT + 1)})


def test_the_endpoint_variables_are_named_with_the_defaults_the_code_has():
    if "FUSEKI_DATA_ENDPOINT" in os.environ or "VM_WRITE_ENDPOINT" in os.environ:
        pytest.skip("an endpoint variable is set in this environment")
    from ingestion import load_ontologies, telemetry_simulator
    text = _text(NOTE)
    for name, default in (("FUSEKI_DATA_ENDPOINT", load_ontologies.DEFAULT_DATA_ENDPOINT),
                          ("VM_WRITE_ENDPOINT", telemetry_simulator.WRITE_ENDPOINT)):
        assert f"`{name}`" in text, name
        assert f"`{default}`" in text, default


# ---- the series limit -------------------------------------------------------------------------


def test_the_note_prints_the_query_form_and_the_conflict_text_the_code_holds():
    text = _text(NOTE)
    assert operations.LIMITED_ROLLUP in text
    assert operations.SERIES_LIMIT_CONFLICT in text
    assert "operations.LIMITED_ROLLUP" in text and "operations.SERIES_LIMIT_CONFLICT" in text
    assert "series_truncated" in text
    assert "limit plus one" in text
    assert "not the worst" in text


def test_the_note_prints_the_default_query_for_the_default_limit():
    selector = 'interface_metrics_input_drops{device="r1"}[5m]'
    default = operations.LIMITED_ROLLUP.format(
        n=validation.tsdb_series_limit({}) + 1, fn="max_over_time", selector=selector)
    assert default in _text(NOTE)


# ---- the proxy --------------------------------------------------------------------------------


def test_the_note_carries_every_word_and_text_of_the_proxy(proxy):
    text = _text(NOTE)
    for word in sorted(proxy.READ_FORMS | proxy.FORBIDDEN_KEYWORDS):
        assert f"`{word}`" in text, word
    for fixed in (proxy.REFUSED_QUERY, proxy.UNSAFE_CALLBACK, proxy.UPSTREAM_FAILED):
        assert fixed in text, fixed
    assert "FROM NAMED" in text
    assert "codepoint escape" in text
    assert "contextbroker-7i2" in text and "open" in _section(text, "The LodLive proxy")


# ---- sessions, reads and the lock -------------------------------------------------------------


def test_the_note_names_each_upstream_and_the_session_rules():
    section = _section(_text(NOTE), "Upstream sessions")
    for name in upstream_http.UPSTREAMS:
        assert f"`{name}`" in section, name
    for phrase in ("no retry", "no cookie", "credential", "timeout", "plain requests", "Qdrant"):
        assert phrase in section, phrase


def test_the_note_lists_what_runs_before_and_inside_the_lock_in_order():
    section = _section(_text(NOTE), "What the per-intent lock covers")
    before, inside = section.index("Before the lock"), section.index("Inside the lock")
    assert before < inside
    steps_before = ("binding read", "state store availability read", "goal check", "mapping check",
                    "telemetry reads", "pushability check")
    positions = [section.index(step, before) for step in steps_before]
    assert positions == sorted(positions)
    steps_inside = ("previous outcome", "marks and regress check", "goal guard", "freshness re-check",
                    "report snapshot", "pushes", "poll", "success re-check", "outcome write")
    positions = [section.index(step, inside) for step in steps_inside]
    assert positions == sorted(positions)
    assert "BROKER_TELEMETRY_WORKERS" in _section(_text(NOTE), "Parallel telemetry reads")
    for path in ("assess_via_tmf921", "assess_tmf921_as_of", "recording()"):
        assert path in _section(_text(NOTE), "Parallel telemetry reads"), path


def test_the_note_names_the_caches_the_agent_result_and_the_public_names():
    from data_contracts import metric_map
    from intent_management import tmf921_client
    text = _text(NOTE)
    for name in ("metric_map_key", "clear_metric_map_cache", "clear_bounds_cache", "AgentResult",
                 "SAFE_TOKEN", "SAFE_WINDOW", "UNIT_SECONDS", "SAFE_RETENTION", "validate_token",
                 "check_token", "check_intent_id", "short_name", "query_tsdb_metrics",
                 "MEASUREMENT_NAME", "read_in_order", "reset_sessions"):
        assert f"`{name}`" in text, name
    assert callable(metric_map.metric_map_key) and callable(metric_map.clear_metric_map_cache)
    assert callable(tmf921_client.clear_bounds_cache)
    caches = _section(text, "Caches")
    assert f"at most {tmf921_client._parse_bounds.cache_info().maxsize} " in caches
    assert f"at most {metric_map._CACHE_MAX} " in caches


# ---- the commands -----------------------------------------------------------------------------


def test_the_note_holds_the_lock_commands_the_digest_read_and_the_real_engine_runs():
    text = _text(NOTE)
    for source, target in (("requirements.in", "requirements.txt"),
                           ("requirements-mcp.in", "requirements-mcp.txt"),
                           ("lodlive-proxy/requirements.in", "lodlive-proxy/requirements.txt")):
        assert (f"pip-compile --generate-hashes --strip-extras --output-file={target} {source}"
                in text), target
    assert "--require-hashes --only-binary=:all:" in text
    assert "auth.docker.io/token" in text and "docker-content-digest" in text
    assert "application/vnd.oci.image.index.v1+json" in text
    assert "VM_PARITY_URL=" in text and "tests/test_vm_parity.py" in text
    assert "FUSEKI_PARITY_URL=" in text and "tests/test_fuseki_parity.py" in text
    assert "contextbroker-lln" in text


def test_the_digests_in_the_note_are_the_ones_the_dockerfiles_pin():
    text = _text(NOTE)
    pinned = set()
    for path in (ROOT / "Dockerfile", ROOT / "lodlive-proxy" / "Dockerfile",
                 ROOT / "docker" / "tmf921" / "Dockerfile"):
        pinned |= set(re.findall(r"@(sha256:[0-9a-f]{64})", path.read_text(encoding="utf-8")))
    assert len(pinned) == 4
    for digest in pinned:
        assert digest in text, digest


# ---- wording and licensing over the note ------------------------------------------------------


def test_wording_the_note_uses_goal_and_never_the_forbidden_words():
    assert FORBIDDEN_WORDS.search(_text(NOTE)) is None


def test_wording_every_fails_closed_in_the_note_names_its_subject():
    text = _text(NOTE)
    assert FAILS_CLOSED.search(text) is not None
    for found in FAILS_CLOSED.finditer(text):
        before = text[:found.start()].rstrip(" \t")
        assert before and before[-1] not in ".!?\n|-*#>", text[max(0, found.start() - 40):found.end()]


def test_wording_the_note_never_uses_the_word_gate():
    assert re.search(r"\bgate\b", _text(NOTE), re.IGNORECASE) is None


def test_licensed_no_marker_of_licensed_material_is_in_the_note():
    text = _text(NOTE).lower()
    for marker in FORBIDDEN_MARKERS:
        assert marker.lower() not in text, marker


def test_the_note_holds_no_secret_value():
    text = _text(NOTE)
    for variable in ("FUSEKI_ADMIN_PASSWORD", "QDRANT_API_KEY"):
        for found in re.finditer(variable + r"\s*[=:]\s*(\S+)", text):
            raise AssertionError(f"{variable} is given a value in the note: {found.group(0)!r}")
