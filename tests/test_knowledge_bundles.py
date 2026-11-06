"""The tracked `knowledge/` directory: the draft example bundles and the example action catalogue.

These tests read the real directory in the repository (not a tmp tree), so they pin what the knowledge
class returns in the later plans: which bundles exist, where each applies, what each cites, and that no
file carries licensed markers or the wording CLAUDE.md forbids.
"""
from pathlib import Path

from artefact_governance.action_catalogue import ActionTemplate
from data_contracts.knowledge import Match, applicable
from okf_retrieval.loader import load_bundles, load_catalogue

REPO_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_ROOT = REPO_ROOT / "knowledge"

# Markers of the licensed material; none may occur in any file under knowledge/ (compared lower-case).
FORBIDDEN_MARKERS = (
    "docs/TMF-921-spec", "TM Forum", "TR290", "TR291", "TR292", "TMF921 specification",
)


def _set():
    return load_bundles(KNOWLEDGE_ROOT)


def test_interface_error_bundle_loads_as_a_draft_with_its_linked_action():
    bundles = _set()
    assert "interface-error-handling" not in bundles.blocked
    bundle = bundles.latest["interface-error-handling"]
    assert bundle.version == "1.0"
    assert bundle.status == "draft"
    assert bundle.linked_actions == ("example-collect-interface-counters",)
    assert bundle.content_hash.startswith("sha256:")


def test_interface_error_bundle_applies_to_a_router_interface_diagnosis_and_not_a_device_alone():
    bundle = _set().latest["interface-error-handling"]
    match = applicable(bundle, device="Router-A", interface="GigabitEthernet0/0/0", purpose="diagnosis")
    assert match == Match(entities=("Router-*",), purpose="diagnosis", ontology_class="Interface")
    assert applicable(bundle, device="Router-A", interface=None, purpose="diagnosis") is None


def test_catalogue_holds_the_approved_example_and_hides_the_unapproved_one():
    catalogue = load_catalogue(KNOWLEDGE_ROOT)
    template = catalogue.get("example-collect-interface-counters")
    assert template == ActionTemplate(
        template_id="example-collect-interface-counters", version="1.0", scope_pattern="Router-*",
        preconditions=("interface exists in the topology",),
        expected_outcome="interface counters are read and recorded",
        recovery="none needed; the action only reads", approved=True)
    assert catalogue.get("example-restart-interface") is None


def test_gitattributes_pins_lf_endings_for_knowledge_files():
    lines = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "knowledge/** text eol=lf" in lines


# ---- All four drafts: load, scope, references, licence and wording ------------------------------------

import fnmatch
import json
import re
import subprocess

import pytest

from data_contracts.knowledge import withheld_by_authority
from okf_retrieval.loader import MAX_BODY_CHARS

IDENTIFIERS = {
    "interface-error-handling", "latency-goal-investigation", "out-of-scope-decoy", "topology-change-check",
}
DECOY = "out-of-scope-decoy"
# Words CLAUDE.md forbids or says to avoid; whole words, case-insensitive.
FORBIDDEN_WORDS = re.compile(r"\b(bound|bounds|drift)\b", re.IGNORECASE)
# A body informs reasoning only; these words would instruct a change to the network.
CHANGE_WORDS = re.compile(r"\b(restart|reconfigure|shutdown)\b|apply the change", re.IGNORECASE)


def _knowledge_files():
    return sorted(path for path in KNOWLEDGE_ROOT.rglob("*") if path.is_file())


def _bundle_files():
    return sorted((KNOWLEDGE_ROOT / "bundles").rglob("*.md"))


def _applicable_ids(device, interface, purpose):
    return {
        identifier for identifier, bundle in _set().latest.items()
        if applicable(bundle, device=device, interface=interface, purpose=purpose) is not None}


def test_exactly_four_draft_bundles_load_with_none_blocked():
    bundles = _set()
    assert set(bundles.latest) == IDENTIFIERS
    assert bundles.blocked == frozenset()
    for bundle in bundles.latest.values():
        assert bundle.version == "1.0"
        assert bundle.status == "draft"
        assert bundle.title
        assert bundle.source_references


def test_every_bundle_file_is_lf_only_without_a_byte_order_mark_and_within_the_body_limit():
    files = _bundle_files()
    assert len(files) == 4
    for path in files:
        raw = path.read_bytes()
        assert b"\r" not in raw
        assert not raw.startswith(b"\xef\xbb\xbf")
        body = raw.decode("utf-8").split("\n---\n", 1)[1].strip()
        assert 0 < len(body) <= MAX_BODY_CHARS


@pytest.mark.parametrize("device, interface, purpose, expected", [
    ("Router-A", "GigabitEthernet0/0/0", "diagnosis",
     {"interface-error-handling", "latency-goal-investigation", "topology-change-check"}),
    ("Router-A", None, "diagnosis", {"topology-change-check"}),
    ("Router-B", "GigabitEthernet0/0/1", "reporting", {"topology-change-check"}),
    ("Switch-Decoy-9", None, "diagnosis", {DECOY}),
])
def test_each_bundle_applies_only_inside_its_declared_scope(device, interface, purpose, expected):
    assert _applicable_ids(device, interface, purpose) == expected


def test_the_decoy_is_withheld_for_a_narrow_token_and_not_for_a_wildcard_token():
    decoy = _set().latest[DECOY]
    kwargs = dict(device="Router-A", interface="GigabitEthernet0/0/0", purpose="diagnosis")
    assert applicable(decoy, **kwargs) is None
    assert withheld_by_authority(decoy, token_entities=frozenset({"Router-A"}), **kwargs) is True
    assert withheld_by_authority(decoy, token_entities=frozenset({"*"}), **kwargs) is False


def test_the_decoy_names_a_device_that_is_not_in_the_demo_topology():
    demo = (REPO_ROOT / "ontologies" / "abox" / "topology-demo.ttl").read_text(encoding="utf-8")
    assert "Switch-Decoy-9" not in demo


def _tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, check=True, capture_output=True, text=True)
    return set(out.stdout.split("\n"))


def test_every_source_reference_resolves_inside_the_repository():
    tracked = _tracked_files()
    metrics = {row["broker_metric"] for row in json.loads(
        (REPO_ROOT / "data_contracts" / "metric_map.json").read_text(encoding="utf-8"))}
    tbox = (REPO_ROOT / "ontologies" / "tbox" / "topology.ttl").read_text(encoding="utf-8")
    classes = set(re.findall(r"^:(\w+) a owl:Class", tbox, re.MULTILINE))
    assert {"Interface", "LinkFluent"} <= classes
    checked = 0
    for bundle in _set().latest.values():
        for reference in bundle.source_references:
            kind, _, target = reference.partition(":")
            if kind == "repo":
                assert target in tracked, reference
            elif kind == "metric":
                assert target in metrics, reference
            else:
                assert kind == "ontology" and target in classes, reference
            checked += 1
    assert checked >= 8


def test_every_linked_action_is_in_the_catalogue_and_every_template_scope_matches_router_a():
    catalogue = load_catalogue(KNOWLEDGE_ROOT)
    for bundle in _set().latest.values():
        for action in bundle.linked_actions:
            assert catalogue.get(action) is not None, action
    text = (KNOWLEDGE_ROOT / "action-catalogue.yaml").read_text(encoding="utf-8")
    patterns = re.findall(r"^\s+scope_pattern: \"([^\"]+)\"$", text, re.MULTILINE)
    assert len(patterns) == 2
    assert all(fnmatch.fnmatchcase("Router-A", pattern) for pattern in patterns)


def test_the_catalogue_holds_exactly_one_approved_and_one_unapproved_example():
    text = (KNOWLEDGE_ROOT / "action-catalogue.yaml").read_text(encoding="utf-8")
    assert re.findall(r"^\s+approved: (true|false)$", text, re.MULTILINE) == ["true", "false"]


def test_no_knowledge_file_carries_a_licensed_marker():
    files = _knowledge_files()
    assert len(files) >= 6
    for path in files:
        text = path.read_text(encoding="utf-8").lower()
        for marker in FORBIDDEN_MARKERS:
            assert marker.lower() not in text, (path.name, marker)


def test_no_knowledge_file_uses_the_wording_claude_md_forbids():
    for path in _knowledge_files():
        assert FORBIDDEN_WORDS.search(path.read_text(encoding="utf-8")) is None, path.name


def test_no_bundle_body_instructs_a_change_to_the_network():
    for path in _bundle_files():
        body = path.read_text(encoding="utf-8").split("\n---\n", 1)[1]
        assert CHANGE_WORDS.search(body) is None, path.name


def test_wording_tests_catch_what_they_look_for():
    assert FORBIDDEN_WORDS.search("the bound was met")
    assert FORBIDDEN_WORDS.search("slow drift")
    assert FORBIDDEN_WORDS.search("the goal was met") is None
    assert CHANGE_WORDS.search("Restart the interface")
    assert CHANGE_WORDS.search("apply the change now")
    assert CHANGE_WORDS.search("read the counters") is None
