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
