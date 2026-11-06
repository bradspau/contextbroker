"""The knowledge vocabulary: pure applicability, entity patterns, latest version, authority withholding."""
import ast
from pathlib import Path

import pytest

from data_contracts import context_contract, knowledge
from data_contracts.knowledge import (
    ENTITY_ANY,
    Bundle,
    KnowledgeScope,
    Match,
    applicable,
    entity_matches,
    latest_per_identifier,
    node_of,
    withheld_by_authority,
)

INTERFACE = "GigabitEthernet0/0/0"


def _bundle(identifier="sample-bundle", version="1.0", status="draft", entities=("Router-*",),
            purposes=("diagnosis",), ontology_classes=()) -> Bundle:
    return Bundle(
        identifier=identifier, version=version, status=status, title="Sample",
        scope=KnowledgeScope(tuple(entities), tuple(purposes), tuple(ontology_classes)),
        source_references=("repo:design/event-store.md",), linked_actions=(),
        content_hash="sha256:" + "0" * 64, embedding_text="Sample\nbody")


def test_the_module_imports_only_the_standard_library_and_the_public_version_key():
    tree = ast.parse(Path(knowledge.__file__).read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
    allowed = {"re", "collections", "collections.abc", "dataclasses",
               "artefact_governance.action_catalogue"}
    assert modules <= allowed
    assert not any("context_contract" in m for m in modules)


def test_the_entity_wildcard_is_the_one_the_contract_uses():
    assert knowledge.ENTITY_ANY == context_contract.ENTITY_WILDCARD


def test_the_closed_sets_and_caps():
    assert knowledge.STATUSES == ("draft", "reviewed", "approved", "withdrawn")
    assert knowledge.RETURNED_STATUSES == ("draft", "reviewed", "approved")
    assert knowledge.ONTOLOGY_CLASSES == ("Device", "Interface")
    assert knowledge.NODE_CLASSES == {"device": "Device", "interface": "Interface"}
    assert knowledge.RETRIEVAL_OUTCOMES == ("answered", "unavailable", "not_current")
    assert (knowledge.KNOWLEDGE_CANDIDATES, knowledge.KNOWLEDGE_RESULT_CAP) == (20, 10)


def test_node_of_follows_the_pinned_node_id_rule():
    assert node_of("Router-A", None) == ("Router-A", "device")
    assert node_of("Router-A", INTERFACE) == (f"Router-A/{INTERFACE}", "interface")


@pytest.mark.parametrize("name", ["Router-A", f"Router-A/{INTERFACE}", "", "anything at all"])
def test_the_whole_wildcard_matches_any_name(name):
    assert entity_matches("*", name)


def test_a_trailing_star_is_a_case_sensitive_prefix():
    assert entity_matches("Router-*", "Router-A")
    assert entity_matches("Router-*", f"Router-A/{INTERFACE}")
    assert not entity_matches("Router-*", "router-a")
    assert not entity_matches("Router-*", "Switch-1")


def test_an_exact_pattern_matches_only_itself():
    assert entity_matches("Router-A", "Router-A")
    assert not entity_matches("Router-A", "Router-AB")
    assert not entity_matches("Router-A", f"Router-A/{INTERFACE}")


@pytest.mark.parametrize("pattern", ["Router-?", "Router-[AB]", "Rou*ter"])
def test_glob_forms_other_than_the_trailing_star_are_not_in_the_grammar(pattern):
    assert not knowledge.ENTITY_PATTERN_RE.fullmatch(pattern)


def test_applicable_tries_both_the_device_and_the_node_id():
    bundle = _bundle(entities=[f"Router-A/{INTERFACE}"])
    assert applicable(bundle, device="Router-A", interface=INTERFACE, purpose="diagnosis") == Match(
        entities=(f"Router-A/{INTERFACE}",), purpose="diagnosis", ontology_class=None)
    assert applicable(bundle, device="Router-A", interface=None, purpose="diagnosis") is None


def test_a_device_pattern_applies_to_an_interface_request_on_that_device():
    bundle = _bundle(entities=["Router-A"])
    assert applicable(bundle, device="Router-A", interface=INTERFACE, purpose="diagnosis") is not None


def test_the_match_lists_only_the_patterns_that_matched_sorted():
    bundle = _bundle(entities=["Switch-*", "Router-*", "*", "Router-A"])
    match = applicable(bundle, device="Router-A", interface=None, purpose="diagnosis")
    assert match.entities == ("*", "Router-*", "Router-A")


def test_a_purpose_that_is_not_declared_does_not_apply():
    assert applicable(_bundle(), device="Router-A", interface=None, purpose="reporting") is None


def test_declared_ontology_classes_are_checked_and_undeclared_are_not():
    only_interface = _bundle(ontology_classes=["Interface"])
    assert applicable(only_interface, device="Router-A", interface=None, purpose="diagnosis") is None
    assert applicable(only_interface, device="Router-A", interface=INTERFACE,
                      purpose="diagnosis").ontology_class == "Interface"
    both = _bundle(ontology_classes=["Device", "Interface"])
    assert applicable(both, device="Router-A", interface=None, purpose="diagnosis").ontology_class == "Device"
    assert applicable(both, device="Router-A", interface=INTERFACE, purpose="diagnosis") is not None
    unconstrained = applicable(_bundle(), device="Router-A", interface=None, purpose="diagnosis")
    assert unconstrained.ontology_class is None


def test_a_withdrawn_bundle_never_applies():
    assert applicable(_bundle(status="withdrawn"), device="Router-A", interface=None,
                      purpose="diagnosis") is None


@pytest.mark.parametrize("status", ["draft", "reviewed", "approved"])
def test_every_returned_status_can_apply(status):
    assert applicable(_bundle(status=status), device="Router-A", interface=None,
                      purpose="diagnosis") is not None


def test_applicable_takes_no_score_or_rank_argument():
    import inspect
    names = set(inspect.signature(applicable).parameters)
    assert names == {"bundle", "device", "interface", "purpose"}


# ---- withheld_by_authority (D-48) ----

def _withheld(bundle, token, device="Router-A", interface=None, purpose="diagnosis"):
    return withheld_by_authority(bundle, device=device, interface=interface, purpose=purpose,
                                 token_entities=frozenset(token))


def test_a_bundle_for_another_device_is_withheld_from_a_token_that_does_not_cover_it():
    assert _withheld(_bundle(entities=["Router-B"]), {"Router-A"}) is True


def test_nothing_is_withheld_from_a_wildcard_token():
    assert _withheld(_bundle(entities=["Router-B"]), {ENTITY_ANY}) is False
    assert _withheld(_bundle(entities=["Router-B"]), {"Router-A", ENTITY_ANY}) is False


def test_a_bundle_whose_purpose_does_not_fit_is_not_withheld():
    assert _withheld(_bundle(entities=["Router-B"]), {"Router-A"}, purpose="reporting") is False


def test_a_bundle_whose_ontology_class_does_not_fit_is_not_withheld():
    bundle = _bundle(entities=["Router-B"], ontology_classes=["Interface"])
    assert _withheld(bundle, {"Router-A"}) is False
    assert _withheld(bundle, {"Router-A"}, interface=INTERFACE) is True


def test_an_applicable_bundle_is_not_withheld():
    assert _withheld(_bundle(), {"Router-A"}) is False


def test_a_bundle_whose_pattern_matches_a_token_entity_is_not_withheld():
    assert _withheld(_bundle(entities=["Router-B*"]), {"Router-A", "Router-B2"}) is False


def test_a_withdrawn_bundle_is_never_withheld():
    assert _withheld(_bundle(entities=["Router-B"], status="withdrawn"), {"Router-A"}) is False


# ---- latest_per_identifier ----

def test_the_latest_version_is_compared_numerically():
    latest = latest_per_identifier([_bundle(version="1.2"), _bundle(version="1.10"), _bundle(version="1.9")])
    assert latest["sample-bundle"].version == "1.10"


def test_a_withdrawn_latest_removes_the_identifier_whatever_came_before():
    latest = latest_per_identifier([_bundle(version="1.0", status="approved"),
                                    _bundle(version="2.0", status="withdrawn")])
    assert latest == {}


def test_a_withdrawn_older_version_is_ignored():
    latest = latest_per_identifier([_bundle(version="1.0", status="withdrawn"),
                                    _bundle(version="2.0", status="approved")])
    assert latest["sample-bundle"].version == "2.0"


def test_versions_that_compare_equal_are_refused():
    with pytest.raises(ValueError):
        latest_per_identifier([_bundle(version="1.0"), _bundle(version="1.00")])


def test_identifiers_are_kept_apart():
    latest = latest_per_identifier([_bundle("first-one"), _bundle("second-one", version="3.0")])
    assert set(latest) == {"first-one", "second-one"}
