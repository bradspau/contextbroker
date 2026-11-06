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
