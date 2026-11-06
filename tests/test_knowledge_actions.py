"""Linked actions come back as approved references only (plan 06-05, OKF-03, D-46, research A6)."""
import dataclasses

from artefact_governance.action_catalogue import ActionCatalogue, ActionTemplate
from okf_retrieval.service import KnowledgeService, resolve_actions
from tests.okf_helpers import FakeEmbedder, TEMPLATE, local_client, write_bundle, write_catalogue


def template(template_id="collect", version="1.0", scope="Router-*", approved=True):
    return ActionTemplate(template_id=template_id, version=version, scope_pattern=scope,
                          preconditions=("interface exists",), expected_outcome="counters are read",
                          recovery="none needed", approved=approved)


def resolve(catalogue, linked, device="Router-A", node_id=None):
    return resolve_actions(catalogue, tuple(linked), device=device, node_id=node_id or device)


def test_an_approved_template_whose_scope_matches_the_device_resolves():
    got, unresolved = resolve(ActionCatalogue([template()]), ["collect"])
    assert [t.template_id for t in got] == ["collect"] and unresolved == 0


def test_an_unapproved_newer_version_leaves_the_approved_latest_in_place():
    catalogue = ActionCatalogue([template(version="1.0"), template(version="2.0", approved=False)])
    got, unresolved = resolve(catalogue, ["collect"])
    assert [(t.template_id, t.version) for t in got] == [("collect", "1.0")] and unresolved == 0


def test_an_unapproved_only_an_unknown_and_an_out_of_scope_template_are_each_unresolved():
    catalogue = ActionCatalogue([template("hidden", approved=False), template("elsewhere", scope="Router-B*")])
    got, unresolved = resolve(catalogue, ["hidden", "no-such-template", "elsewhere"])
    assert got == () and unresolved == 3


def test_an_interface_scoped_pattern_resolves_for_the_interface_node_only():
    catalogue = ActionCatalogue([template(scope="Router-A/Gig*")])
    node = "Router-A/GigabitEthernet0/0/0"
    assert [t.template_id for t in resolve(catalogue, ["collect"], node_id=node)[0]] == ["collect"]
    assert resolve(catalogue, ["collect"]) == ((), 1)


def test_resolved_templates_keep_the_declared_order_and_the_count_is_exact():
    catalogue = ActionCatalogue([template("alpha"), template("bravo"), template("charlie", approved=False)])
    got, unresolved = resolve(catalogue, ["bravo", "charlie", "alpha", "delta"])
    assert [t.template_id for t in got] == ["bravo", "alpha"] and unresolved == 2


def test_no_linked_actions_resolve_to_nothing():
    assert resolve(ActionCatalogue([template()]), []) == ((), 0)


def test_a_retrieved_reference_carries_the_approved_template_and_counts_the_unapproved_one(tmp_path):
    root = tmp_path / "k"
    write_bundle(root, "interface-error", entities=("Router-*",), purposes=("diagnosis",),
                 linked_actions=("example-collect-interface-counters", "example-restart-interface"))
    write_catalogue(root, [TEMPLATE, dict(TEMPLATE, template_id="example-restart-interface", approved=False)])
    service = KnowledgeService(root=root, client_factory=local_client, embedder_factory=FakeEmbedder)
    got = service.retrieve(device="Router-A", interface=None, purpose="diagnosis", topic=None,
                           token_entities=frozenset({"*"}))
    assert got.outcome == "answered" and len(got.references) == 1
    ref = got.references[0]
    assert [(t.template_id, t.version) for t in ref.actions] == [("example-collect-interface-counters", "1.0")]
    assert ref.unresolved_actions == 1
    assert all(isinstance(t, ActionTemplate) and t.approved is True for t in ref.actions)


def test_a_template_is_frozen_so_a_reference_cannot_change_the_catalogue():
    t = template()
    try:
        t.approved = False
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("ActionTemplate must be frozen")
