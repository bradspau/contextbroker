"""Pins between the tracked design notes of Phase 7 and the code (plan 07-08).

A note that disagrees with the code misleads whoever integrates against it, so the parts that must match
are read from the note and compared with the code: the printed JSON-LD context, the rule names, the
settings and their defaults, the refusal texts and the neighbour-discovery format names. The wording
rules of CLAUDE.md and the licensed-marker rule are checked over both new notes.
"""
import json
import re
from pathlib import Path

import pytest

from broker_state.process import load_settings
from data_contracts import exchange_profile, promotion
from ingestion import promote

ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / "design"
EXCHANGE_NOTE = DESIGN / "exchange-profile.md"
PROMOTION_NOTE = DESIGN / "semantic-promotion.md"
NEW_NOTES = (EXCHANGE_NOTE, PROMOTION_NOTE)

# The word CLAUDE.md forbids in place of goal, and the one it says to avoid; whole words, any case.
FORBIDDEN_WORDS = re.compile(r"\b(bound|bounds|drift)\b", re.IGNORECASE)
# Licensed material may never be quoted or cited; the same markers tests/test_knowledge_bundles.py uses.
FORBIDDEN_MARKERS = (
    "docs/TMF-921-spec", "TM Forum", "TR290", "TR291", "TR292", "TMF921 specification",
)
FAILS_CLOSED = re.compile(r"fails closed", re.IGNORECASE)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json_block_after(text: str, heading: str) -> str:
    """The first fenced json block after a heading line."""
    start = text.index("\n" + heading + "\n")
    found = re.search(r"```json\n(.*?)\n```", text[start:], re.DOTALL)
    assert found is not None, f"no json block after {heading!r}"
    return found.group(1)


# ---- the exchange profile note (task 1) -------------------------------------------------------


def test_the_printed_context_is_the_context_the_code_emits():
    printed = json.loads(_json_block_after(_text(EXCHANGE_NOTE), "## The context"))
    assert printed == exchange_profile.CONTEXT


def test_the_printed_context_is_the_indented_dump_so_a_reader_can_diff_it():
    block = _json_block_after(_text(EXCHANGE_NOTE), "## The context")
    assert block == json.dumps(exchange_profile.CONTEXT, indent=2)


def test_the_exchange_note_names_the_namespace_the_media_type_and_the_checks():
    text = _text(EXCHANGE_NOTE)
    for constant in (exchange_profile.NAMESPACE, exchange_profile.BASE_IRI,
                     exchange_profile.PROFILE_MEDIA_TYPE):
        assert constant in text, constant
    assert "known-answer" in text
    assert "rdflib" in text


def test_every_type_path_of_the_code_is_in_the_exchange_note():
    text = _text(EXCHANGE_NOTE)
    for path, type_, _id_key in exchange_profile.TYPE_PATHS:
        assert type_ in text, type_
        if path:
            assert path in text, path


def test_the_exchange_note_names_the_limits_of_the_accept_parser():
    text = _text(EXCHANGE_NOTE)
    assert str(exchange_profile.MAX_ACCEPT_LENGTH) in text
    assert str(exchange_profile.MAX_ACCEPT_ITEMS) in text


def test_the_contract_note_points_at_the_exchange_note_and_defers_ngsi_ld():
    text = _text(DESIGN / "context-contract.md")
    assert "exchange-profile.md" in text
    assert "NGSI-LD" in text
    assert "are Phase 7 and need no change" not in text
    assert "JSON-LD and NGSI-LD views of the Context Graph: Phase 7" not in text
    assert "promoted_conflicts" in text
    assert "hypotheses" in text


# ---- the semantic promotion note (task 2) -----------------------------------------------------


def _human_duration(seconds: int) -> str:
    """A setting in the grammar the settings use: whole days, else hours, else minutes."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _setting_rows(text: str) -> dict[str, str]:
    """Each `| `VARIABLE` | default | ... |` row of a note's configuration table: variable -> default."""
    rows = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"`BROKER_[A-Z_]+`", cells[0]):
            rows[cells[0].strip("`")] = cells[1].strip("`")
    return rows


def _promotion_defaults() -> dict[str, str]:
    settings = load_settings({})
    return {
        "BROKER_PROMOTED_CAP": str(settings.promoted_cap),
        "BROKER_PROMOTED_RETENTION": _human_duration(settings.promoted_retention_s),
        "BROKER_PROMOTE_MIN_EVENTS": str(settings.promote_min_events),
        "BROKER_PROMOTE_WINDOW": _human_duration(settings.promote_window_s),
    }


def test_the_defaults_this_test_expects_are_the_ones_the_plan_names():
    assert _promotion_defaults() == {
        "BROKER_PROMOTED_CAP": "10000", "BROKER_PROMOTED_RETENTION": "30d",
        "BROKER_PROMOTE_MIN_EVENTS": "3", "BROKER_PROMOTE_WINDOW": "15m",
    }


def test_the_promotion_note_names_every_rule_and_the_source_and_the_command():
    text = _text(PROMOTION_NOTE)
    for rule in promotion.PROMOTION_RULES:
        assert rule in text, rule
    assert promotion.PROMOTION_SOURCE in text
    assert "python -m ingestion.promote --situation" in text
    for kind in promotion.PROMOTED_KINDS:
        assert f"`{kind}`" in text, kind


def test_the_promotion_note_lists_each_setting_with_the_default_the_code_gives():
    rows = _setting_rows(_text(PROMOTION_NOTE))
    for name, default in _promotion_defaults().items():
        assert rows.get(name) == default, (name, rows.get(name), default)


def test_the_state_note_has_a_configuration_row_for_each_setting():
    text = _text(DESIGN / "durable-state.md")
    rows = _setting_rows(text)
    for name, default in _promotion_defaults().items():
        assert rows.get(name) == default, (name, rows.get(name), default)
    assert "promoted" in text


def test_the_promotion_note_carries_every_refusal_text_of_the_command():
    texts = {value for name, value in vars(promote).items()
             if name.startswith("REFUSED_") and isinstance(value, str)}
    assert len(texts) >= 6
    text = _text(PROMOTION_NOTE)
    for refusal in sorted(texts):
        assert refusal in text, refusal


def test_the_promotion_note_names_the_approved_formats_or_says_plumbing_only():
    text = _text(PROMOTION_NOTE)
    try:
        from data_contracts import adjacency
    except ImportError:
        assert "plumbing only" in text
        return
    assert adjacency.ADJACENCY_FORMATS
    for name in adjacency.ADJACENCY_FORMATS:
        assert name in text, name


def test_the_promotion_note_says_a_hypothesis_is_inferred_and_never_evidence():
    text = _text(PROMOTION_NOTE)
    assert promotion.HYPOTHESIS_LABEL in text
    assert promotion.HYPOTHESIS_STATE in text
    for state in promotion.SUPPORT_STATES:
        assert f"`{state}`" in text, state
    assert "hypotheses_truncated" in text
    assert "hypotheses_withheld" in text
    assert "evicted_promoted" in text


def test_the_event_note_no_longer_defers_inferred_state_to_a_later_phase():
    text = _text(DESIGN / "event-store.md")
    assert "That belongs to Phase 7" not in text
    assert "semantic-promotion.md" in text


# ---- the wording and licensing rules over both new notes --------------------------------------


@pytest.mark.parametrize("path", NEW_NOTES, ids=lambda p: p.name)
def test_wording_the_new_notes_use_goal_and_never_the_forbidden_words(path):
    assert FORBIDDEN_WORDS.search(_text(path)) is None


@pytest.mark.parametrize("path", NEW_NOTES, ids=lambda p: p.name)
def test_wording_every_fails_closed_names_its_subject(path):
    text = _text(path)
    for found in FAILS_CLOSED.finditer(text):
        before = text[:found.start()].rstrip(" \t")
        assert before and before[-1] not in ".!?\n|-*#>", text[max(0, found.start() - 40):found.end()]


@pytest.mark.parametrize("path", NEW_NOTES, ids=lambda p: p.name)
def test_wording_the_action_readiness_check_is_never_called_a_gate(path):
    assert re.search(r"readiness[ -]check[^.\n]{0,40}\bgate\b|\bgate\b[^.\n]{0,40}readiness",
                     _text(path), re.IGNORECASE) is None


@pytest.mark.parametrize("path", NEW_NOTES, ids=lambda p: p.name)
def test_licensed_no_marker_of_licensed_material_is_in_the_new_notes(path):
    text = _text(path).lower()
    for marker in FORBIDDEN_MARKERS:
        assert marker.lower() not in text, (path.name, marker)


def test_wording_the_checks_catch_what_they_look_for():
    assert FORBIDDEN_WORDS.search("the upper bound") and FORBIDDEN_WORDS.search("slow drift")
    assert FORBIDDEN_WORDS.search("the goal and the bounded read") is None
    assert FAILS_CLOSED.search("fails closed")


# ---- README, CLAUDE.md and the gap analysis (task 3) ------------------------------------------


def test_nothing_still_calls_the_mcp_server_not_mounted():
    for path in (ROOT / "README.md", DESIGN / "context-contract.md", DESIGN / "layer3-gap-analysis.md"):
        assert "not mounted" not in _text(path).lower(), path.name


def test_the_readme_says_the_mcp_server_is_not_served_and_an_optional_extra():
    text = _text(ROOT / "README.md")
    assert "not served" in text
    assert "pip install -r requirements-mcp.txt" in text
    assert "Phase 7 decides whether it is served" not in text


def test_the_readme_and_claude_md_describe_the_profile_and_the_promotion_command():
    readme, claude = _text(ROOT / "README.md"), _text(ROOT / "CLAUDE.md")
    assert "application/ld+json" in readme and "application/ld+json" in claude
    assert "ingestion.promote" in readme and "ingestion.promote" in claude
    assert "requirements-mcp.txt" in claude
    for note in ("design/exchange-profile.md", "design/semantic-promotion.md"):
        assert note in readme, note
    for name in ("exchange-profile.md", "semantic-promotion.md"):
        assert name in claude, name


def test_the_notes_keep_the_health_keys_at_eight():
    assert "eight keys" in _text(PROMOTION_NOTE)
    assert "eight keys" in _text(ROOT / "CLAUDE.md")
