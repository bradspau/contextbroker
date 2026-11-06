"""The knowledge loader: a reviewed bundle file becomes a bundle, or is refused with a fixed reason."""
import hashlib

from data_contracts.knowledge import Match, applicable
from artefact_governance.action_catalogue import _version_key, version_key
from okf_retrieval.loader import load_bundles
from tests.okf_helpers import write_bundle

INTERFACE = dict(
    entities=["Router-*"], purposes=["diagnosis", "assurance"], ontology_classes=["Interface"],
    source_references=["repo:design/event-store.md"],
    linked_actions=["example-collect-interface-counters"])


def _write_interface(tmp_path):
    return write_bundle(tmp_path, "interface-error-handling", "1.0", **INTERFACE)


def test_a_bundle_file_is_read_with_its_exact_identity(tmp_path):
    path = _write_interface(tmp_path)
    loaded = load_bundles(tmp_path)
    assert loaded.blocked == frozenset()
    assert list(loaded.latest) == ["interface-error-handling"]
    bundle = loaded.latest["interface-error-handling"]
    assert bundle.identifier == "interface-error-handling"
    assert bundle.version == "1.0"
    assert bundle.status == "draft"
    assert bundle.title == "Interface error handling"
    assert bundle.scope.entities == ("Router-*",)
    assert bundle.scope.purposes == ("diagnosis", "assurance")
    assert bundle.scope.ontology_classes == ("Interface",)
    assert bundle.source_references == ("repo:design/event-store.md",)
    assert bundle.linked_actions == ("example-collect-interface-counters",)
    assert bundle.content_hash == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert bundle.embedding_text.startswith("Interface error handling\n")


def test_the_content_hash_is_stable_and_changes_with_one_byte(tmp_path):
    path = _write_interface(tmp_path)
    first = load_bundles(tmp_path).latest["interface-error-handling"].content_hash
    assert load_bundles(tmp_path).latest["interface-error-handling"].content_hash == first
    path.write_bytes(path.read_bytes().replace(b"counters", b"counterz"))
    assert load_bundles(tmp_path).latest["interface-error-handling"].content_hash != first


def test_a_matching_request_gets_a_match_and_no_other_does(tmp_path):
    _write_interface(tmp_path)
    bundle = load_bundles(tmp_path).latest["interface-error-handling"]
    assert applicable(bundle, device="Router-A", interface="GigabitEthernet0/0/0",
                      purpose="diagnosis") == Match(
        entities=("Router-*",), purpose="diagnosis", ontology_class="Interface")
    assert applicable(bundle, device="Router-A", interface=None, purpose="diagnosis") is None
    assert applicable(bundle, device="Router-A", interface="GigabitEthernet0/0/0",
                      purpose="reporting") is None
    assert applicable(bundle, device="Switch-1", interface="GigabitEthernet0/0/0",
                      purpose="diagnosis") is None


def test_version_order_is_numeric_and_the_public_name_is_the_private_function():
    assert version_key is _version_key
    assert version_key("1.10") > version_key("1.2")
