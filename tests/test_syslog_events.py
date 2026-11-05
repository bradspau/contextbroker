"""The shared event vocabulary: the event id and its module boundary."""
import ast
import hashlib
from pathlib import Path

from data_contracts import syslog_events
from data_contracts.syslog_events import EVENT_ID_PATTERN, event_id

STDLIB_ONLY = {"__future__", "dataclasses", "datetime", "hashlib", "re", "typing"}


def test_event_id_is_evt_plus_32_hex_of_sha256_over_the_raw_line():
    raw = "<13>1 - h a - - - x"
    assert event_id(raw) == "evt-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def test_event_id_is_stable_and_sensitive_to_one_character():
    a = "<13>1 - h a - - - x"
    assert event_id(a) == event_id(a)
    assert event_id(a) != event_id(a[:-1] + "y")


def test_event_id_matches_its_pattern_and_not_with_a_trailing_newline():
    i = event_id("<13>1 - h a - - - x")
    assert EVENT_ID_PATTERN.match(i)
    assert not EVENT_ID_PATTERN.match(i + "\n")
    assert not EVENT_ID_PATTERN.match(i.upper())


def test_module_imports_only_the_standard_library():
    tree = ast.parse(Path(syslog_events.__file__).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            roots.add(node.module.split(".")[0])
    assert roots <= STDLIB_ONLY, roots
