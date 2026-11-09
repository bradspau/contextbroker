"""The closed promotion vocabulary and the response models that carry promoted records (SEM-01).

A promoted record never changes configured state and never counts as evidence (D-64); a hypothesis is
labelled "hypothesis" with state "inferred" and sits in a list of its own, never in the configured edges.
"""
import ast
import re
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from data_contracts import context_contract as cc
from data_contracts import promotion as pr
from data_contracts.envelope import StateCategory

PROMOTION_SOURCE_FILE = Path(pr.__file__)

# Known answers, computed once from the construction in promoted_id() and pinned here, because the state
# file will store these ids and a change to the construction is a PROMOTION_VERSION bump.
CONFLICT_ID_ROUTER_A = "prm-650df0001ae3f12dba2a4c034657de63"
HYPOTHESIS_ID_PAIR = "prm-5c276cb6e2113785f1ed641082b26545"
PAIR = ("Router-A/GigabitEthernet0/0/1", "Router-C/GigabitEthernet0/0/2")


def _provenance():
    return cc.Provenance(source_system=pr.PROMOTION_SOURCE, reference="prm-" + "0" * 32,
                         observed_at=None, retrieved_at="2026-11-06T12:00:00+00:00")


# ---- the vocabulary ----

def test_the_closed_sets():
    assert pr.PROMOTION_VERSION == 1
    assert pr.PROMOTED_KINDS == ("conflict", "hypothesis")
    assert pr.PROMOTION_RULES == ("repeated_link_down_vs_configured", "adjacency_without_configured_link")
    assert pr.RULE_REPEATED_LINK_DOWN == "repeated_link_down_vs_configured"
    assert pr.RULE_ADJACENCY == "adjacency_without_configured_link"
    assert dict(pr.RULE_KIND) == {pr.RULE_REPEATED_LINK_DOWN: "conflict", pr.RULE_ADJACENCY: "hypothesis"}
    assert pr.PROMOTION_SOURCE == "broker-promotion"
    assert pr.SUPPORT_KINDS == ("situation", "event")
    assert pr.SUPPORT_STATES == ("present", "evicted", "withheld")
    assert pr.PROMOTED_SUPPORT_MAX == 20
    assert pr.HYPOTHESIS_LABEL == "hypothesis"
    assert pr.HYPOTHESIS_STATE == StateCategory.INFERRED.value


def test_the_rule_kind_mapping_is_read_only():
    with pytest.raises(TypeError):
        pr.RULE_KIND["new_rule"] = "conflict"


def test_a_promoted_id_is_pinned_stable_and_distinct():
    conflict = pr.promoted_id("conflict", pr.RULE_REPEATED_LINK_DOWN, ("Router-A",))
    assert pr.PROMOTED_ID_PATTERN.match(conflict)
    assert conflict == pr.promoted_id("conflict", pr.RULE_REPEATED_LINK_DOWN, ("Router-A",))
    assert conflict != pr.promoted_id("conflict", pr.RULE_REPEATED_LINK_DOWN, ("Router-B",))
    assert conflict == CONFLICT_ID_ROUTER_A
    hypothesis = pr.promoted_id("hypothesis", pr.RULE_ADJACENCY, PAIR)
    assert pr.PROMOTED_ID_PATTERN.match(hypothesis)
    assert hypothesis == HYPOTHESIS_ID_PAIR


def test_the_id_pattern_refuses_a_trailing_newline_and_other_shapes():
    good = CONFLICT_ID_ROUTER_A
    assert pr.PROMOTED_ID_PATTERN.match(good)
    for bad in (good + "\n", good.upper(), good[:-1], "evt-" + good[4:], "sit-" + good[4:]):
        assert not pr.PROMOTED_ID_PATTERN.match(bad)


@pytest.mark.parametrize("args", [
    ("hypothesis", pr.RULE_REPEATED_LINK_DOWN, ("Router-A",)),   # the rule belongs to the other kind
    ("conflict", pr.RULE_ADJACENCY, ("Router-A",)),
    ("fact", pr.RULE_REPEATED_LINK_DOWN, ("Router-A",)),          # unknown kind
    ("conflict", "any_rule", ("Router-A",)),                      # unknown rule
    ("conflict", pr.RULE_REPEATED_LINK_DOWN, ()),                 # empty subject
    ("conflict", pr.RULE_REPEATED_LINK_DOWN, ("",)),              # empty element
    ("conflict", pr.RULE_REPEATED_LINK_DOWN, ("Router-A", 3)),    # a non-string
    ("conflict", pr.RULE_REPEATED_LINK_DOWN, ["Router-A"]),       # not a tuple
    ("conflict", pr.RULE_REPEATED_LINK_DOWN, "Router-A"),
])
def test_promoted_id_refuses_what_the_vocabulary_does_not_allow(args):
    with pytest.raises(ValueError):
        pr.promoted_id(*args)


def test_the_vocabulary_module_is_pure():
    tree = ast.parse(PROMOTION_SOURCE_FILE.read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names)
    source = PROMOTION_SOURCE_FILE.read_text()
    assert "datetime.now" not in source and "time.time" not in source
    assert not re.search(r"^\s*(from|import)\s+data_contracts", source, re.M)


# ---- the support reference ----

def test_a_withheld_support_has_no_id_and_is_only_for_a_situation():
    ref = cc.SupportRef(kind="situation", id=None, state="withheld")
    assert ref.id is None
    with pytest.raises(ValidationError):
        cc.SupportRef(kind="event", id=None, state="withheld")
    with pytest.raises(ValidationError):
        cc.SupportRef(kind="situation", id="sit-" + "0" * 32, state="withheld")
    with pytest.raises(ValidationError):
        cc.SupportRef(kind="event", id=None, state="present")
    with pytest.raises(ValidationError):
        cc.SupportRef(kind="situation", id=None, state="evicted")


def test_a_present_or_evicted_support_names_its_target():
    assert cc.SupportRef(kind="event", id="evt-" + "0" * 32, state="evicted").state == "evicted"
    assert cc.SupportRef(kind="situation", id="sit-" + "0" * 32, state="present").kind == "situation"
    with pytest.raises(ValidationError):
        cc.SupportRef(kind="alert", id="x", state="present")
    with pytest.raises(ValidationError):
        cc.SupportRef(kind="event", id="x", state="lost")


# ---- the hypothesis edge and the promoted conflict ----

def _hypothesis(**over):
    fields = dict(promoted_id=HYPOTHESIS_ID_PAIR, from_=PAIR[0], to=PAIR[1], label="hypothesis",
                  state="inferred", rule=pr.RULE_ADJACENCY, valid_from="2026-11-06T11:00:00+00:00",
                  promoted_at="2026-11-06T12:00:00+00:00",
                  support=[cc.SupportRef(kind="situation", id=None, state="withheld")],
                  provenance=_provenance())
    fields.update(over)
    return cc.HypothesisEdge(**fields)


def _promoted_conflict(**over):
    fields = dict(promoted_id=CONFLICT_ID_ROUTER_A, rule=pr.RULE_REPEATED_LINK_DOWN, device="Router-A",
                  event="link_down", configured="link_in_force", text="event link_down contradicts configured "
                  "link_in_force", valid_from="2026-11-06T11:00:00+00:00",
                  promoted_at="2026-11-06T12:00:00+00:00",
                  support=[cc.SupportRef(kind="event", id="evt-" + "0" * 32, state="evicted")],
                  provenance=_provenance())
    fields.update(over)
    return cc.PromotedConflictOut(**fields)


def test_a_hypothesis_dumps_by_alias_with_from_and_never_from_underscore():
    dumped = _hypothesis().model_dump(by_alias=True)
    assert dumped["from"] == PAIR[0] and "from_" not in dumped
    assert dumped["label"] == "hypothesis" and dumped["state"] == "inferred"


@pytest.mark.parametrize("over", [
    {"label": "fact"}, {"label": "configured"}, {"state": "observed"}, {"state": "configured"},
    {"rule": pr.RULE_REPEATED_LINK_DOWN}, {"rule": "any_rule"},
])
def test_a_hypothesis_cannot_look_like_anything_but_a_hypothesis(over):
    with pytest.raises(ValidationError):
        _hypothesis(**over)


def test_a_promoted_conflict_refuses_another_rule_and_an_extra_key():
    assert _promoted_conflict().rule == pr.RULE_REPEATED_LINK_DOWN
    with pytest.raises(ValidationError):
        _promoted_conflict(rule=pr.RULE_ADJACENCY)
    with pytest.raises(ValidationError):
        _promoted_conflict(event="link_sideways")
    with pytest.raises(ValidationError):
        _promoted_conflict(configured="link_absent")
    with pytest.raises(ValidationError):
        cc.PromotedConflictOut(**{**_promoted_conflict().model_dump(), "raw": "x"})


# ---- the places they sit, and what did not change ----

def _graph(**extra):
    limits = cc.GraphLimits(requested_depth=1, max_depth=3, node_cap=50,
                            relationship_types=["connected_to"], cut_by=[])
    return cc.ContextGraph(nodes=[], edges=[], limits=limits, **extra)


def test_the_graph_still_validates_with_three_arguments_and_defaults_the_new_fields():
    dumped = _graph().model_dump(by_alias=True)
    assert dumped["hypotheses"] == [] and dumped["hypotheses_truncated"] is False
    assert dumped["hypotheses_withheld"] == 0
    with pytest.raises(ValidationError):
        _graph(hypotheses_withheld=-1)
    graph = _graph(hypotheses=[_hypothesis()], hypotheses_truncated=True, hypotheses_withheld=2)
    assert graph.hypotheses[0].label == "hypothesis" and graph.edges == []


def test_the_events_result_defaults_promoted_conflicts_to_empty():
    window = cc.EventWindow(start="2026-11-06T11:00:00+00:00", end="2026-11-06T12:00:00+00:00", seconds=3600)
    empty = cc.EventsResult(window=window, cap=100, truncated=False, events=[])
    assert empty.model_dump()["promoted_conflicts"] == []
    full = cc.EventsResult(window=window, cap=100, truncated=False, events=[],
                           promoted_conflicts=[_promoted_conflict()])
    assert full.promoted_conflicts[0].device == "Router-A"


def test_a_hypothesis_is_not_a_configured_relationship():
    assert cc.RELATIONSHIP_TYPES == ("has_interface", "connected_to")
    assert cc.SITUATION_VERSION == 1
    assert cc.HYPOTHESIS_RESULT_CAP == 50
    with pytest.raises(ValidationError):
        cc.GraphEdge(from_="a", to="b", type="hypothesis", link=None, fluent=None)
    request_fields = cc.ContextRequest.model_fields["relationship_types"]
    assert "hypothesis" not in str(request_fields.annotation)


def test_no_new_field_description_names_a_device_or_host():
    for model in (cc.SupportRef, cc.PromotedConflictOut, cc.HypothesisEdge, cc.ContextGraph, cc.EventsResult):
        for name, field in model.model_fields.items():
            text = field.description or ""
            assert "Router-" not in text and "olt1" not in text, (model.__name__, name)
