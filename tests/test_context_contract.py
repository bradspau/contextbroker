"""The logical context contract: request model, authority check, situation identity, times, expiry and
the response models (CTX-01, CTX-02, CTX-03, CTX-05, and the structures for CTX-06 and CTX-07).

No test here touches the network, a clock or FastAPI: the contract module is pure (D-06).
"""
import subprocess
import sys
import typing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from data_contracts import context_contract as cc
from data_contracts.context_contract import (
    CONTEXT_CLASSES, PURPOSES, RELATIONSHIP_TYPES, REFUSED_CLASS, REFUSED_ENTITY, REFUSED_PURPOSE,
    Authority, ContextRequest, authorize, situation_id,
)

REPO = Path(__file__).resolve().parent.parent

TOPOLOGY_BODY = {"purpose": "diagnosis", "scope": {"device": "Router-A"}, "classes": ["topology"],
                 "max_depth": 2, "relationship_types": ["connected_to", "has_interface"]}
GOALS_BODY = {"purpose": "assurance", "scope": {"device": "olt1", "interface": "pon0"},
              "intent_id": "hsi-demo-1", "classes": ["goals", "observations"]}


def _body(base, **changes):
    body = {**base, "scope": dict(base["scope"])}
    for key, value in changes.items():
        if value is _DROP:
            body.pop(key, None)
        else:
            body[key] = value
    return body


_DROP = object()


# ---- the request model ----

def test_a_topology_body_parses():
    request = ContextRequest.model_validate_json(_json(TOPOLOGY_BODY))
    assert request.purpose == "diagnosis"
    assert request.scope.device == "Router-A" and request.scope.interface is None
    assert request.classes == ["topology"]
    assert request.max_depth == 2
    assert request.relationship_types == ["connected_to", "has_interface"]
    assert request.intent_id is None and request.as_of is None and request.known_at is None


def test_a_goals_body_parses_without_topology_fields():
    request = ContextRequest.model_validate_json(_json(GOALS_BODY))
    assert request.intent_id == "hsi-demo-1"
    assert request.scope.interface == "pon0"
    assert request.classes == ["goals", "observations"]
    assert request.max_depth is None and request.relationship_types is None


def test_a_dict_body_parses_too():
    # FastAPI hands the handler a parsed dict, not JSON text.
    assert ContextRequest.model_validate(GOALS_BODY).intent_id == "hsi-demo-1"


def test_all_three_classes_parse_together():
    body = _body(GOALS_BODY, classes=["topology", "goals", "observations"], max_depth=1,
                 relationship_types=["has_interface"], as_of="10m", known_at="1h", freshness="2m")
    request = ContextRequest.model_validate(body)
    assert (request.as_of, request.known_at, request.freshness) == ("10m", "1h", "2m")


def _json(body):
    import json
    return json.dumps(body)


def _scope(**changes):
    return {"device": "Router-A", **changes}


REFUSED_BODIES = {
    "requester field": _body(TOPOLOGY_BODY, requester="alice"),
    "unknown top-level key": _body(TOPOLOGY_BODY, extra="x"),
    "unknown scope key": _body(TOPOLOGY_BODY, scope=_scope(vrf="x")),
    "missing purpose": _body(TOPOLOGY_BODY, purpose=_DROP),
    "missing scope": _body(TOPOLOGY_BODY, scope=_DROP),
    "missing classes": _body(TOPOLOGY_BODY, classes=_DROP),
    "empty classes": _body(TOPOLOGY_BODY, classes=[]),
    "unknown purpose": _body(TOPOLOGY_BODY, purpose="admin"),
    "unknown class": _body(TOPOLOGY_BODY, classes=["events"]),
    "device with a space": _body(TOPOLOGY_BODY, scope=_scope(device="Router A")),
    "empty device": _body(TOPOLOGY_BODY, scope=_scope(device="")),
    "device with a trailing newline": _body(TOPOLOGY_BODY, scope=_scope(device="R\n")),
    "device of 129 characters": _body(TOPOLOGY_BODY, scope=_scope(device="a" * 129)),
    "missing device": _body(TOPOLOGY_BODY, scope={}),
    "interface with a space": _body(TOPOLOGY_BODY, scope=_scope(interface="a b")),
    "interface with a trailing newline": _body(TOPOLOGY_BODY, scope=_scope(interface="a\n")),
    "intent id with a space": _body(GOALS_BODY, intent_id="bad id"),
    "intent id of 65 characters": _body(GOALS_BODY, intent_id="a" * 65),
    "intent id with a trailing newline": _body(GOALS_BODY, intent_id="abc\n"),
    "as_of without a unit": _body(TOPOLOGY_BODY, as_of="10"),
    "as_of with a trailing newline": _body(TOPOLOGY_BODY, as_of="10m\n"),
    "known_at without a unit": _body(TOPOLOGY_BODY, known_at="10"),
    "freshness with an unknown unit": _body(TOPOLOGY_BODY, freshness="2x"),
    "depth 4": _body(TOPOLOGY_BODY, max_depth=4),
    "negative depth": _body(TOPOLOGY_BODY, max_depth=-1),
    "string depth": _body(TOPOLOGY_BODY, max_depth="2"),
    "bool depth": _body(TOPOLOGY_BODY, max_depth=True),
    "float depth": _body(TOPOLOGY_BODY, max_depth=2.0),
    "unknown relationship type": _body(TOPOLOGY_BODY, relationship_types=["peers"]),
    "repeated class": _body(TOPOLOGY_BODY, classes=["topology", "topology"]),
    "repeated relationship type": _body(TOPOLOGY_BODY, relationship_types=["connected_to", "connected_to"]),
    "topology without max_depth": _body(TOPOLOGY_BODY, max_depth=_DROP),
    "topology without relationship types": _body(TOPOLOGY_BODY, relationship_types=_DROP),
    "max_depth without topology": _body(GOALS_BODY, max_depth=1),
    "relationship types without topology": _body(GOALS_BODY, relationship_types=["has_interface"]),
    "goals without an intent": _body(GOALS_BODY, intent_id=_DROP),
    "intent without goals": _body(TOPOLOGY_BODY, intent_id="hsi-demo-1"),
    "observations without goals": _body(GOALS_BODY, classes=["observations"]),
}


@pytest.mark.parametrize("name", sorted(REFUSED_BODIES))
def test_a_malformed_or_incoherent_body_is_refused(name):
    with pytest.raises(ValidationError):
        ContextRequest.model_validate_json(_json(REFUSED_BODIES[name]))
    with pytest.raises(ValidationError):
        ContextRequest.model_validate(REFUSED_BODIES[name])


def test_the_closed_sets_have_one_definition():
    fields = ContextRequest.model_fields
    assert typing.get_args(fields["purpose"].annotation) == PURPOSES
    assert typing.get_args(typing.get_args(fields["classes"].annotation)[0]) == CONTEXT_CLASSES
    relationship = [a for a in typing.get_args(fields["relationship_types"].annotation) if a is not type(None)][0]
    assert typing.get_args(typing.get_args(relationship)[0]) == RELATIONSHIP_TYPES


def test_the_limits_are_pinned():
    assert cc.MAX_DEPTH == 3 and cc.NODE_CAP == 50
    assert cc.RELATIONSHIP_TYPES == ("has_interface", "connected_to")
    assert cc.CUT_REASONS == ("authority", "depth", "node_cap", "relationship_types")
    assert cc.RESULTS == ("eligible", "ineligible", "gap")


def test_node_cap_equals_the_tmf921_entity_cap():
    from intent_management.tmf921_source import DEFAULT_MAX_ENTITIES
    assert cc.NODE_CAP == DEFAULT_MAX_ENTITIES


# ---- authorize ----

def _request(base=TOPOLOGY_BODY, **changes):
    return ContextRequest.model_validate(_body(base, **changes))


def _authority(entities=("Router-A",), purposes=("diagnosis",), classes=("topology",)):
    return Authority(frozenset(entities), frozenset(purposes), frozenset(classes))


def test_authorize_accepts_a_request_inside_authority():
    assert authorize(_request(), _authority()) is None


def test_authorize_refuses_each_dimension_with_a_fixed_detail():
    assert authorize(_request(scope={"device": "Router-B"}), _authority()) == "outside authority scope: entity"
    assert authorize(_request(purpose="reporting"), _authority()) == "outside authority scope: purpose"
    both = _request(GOALS_BODY, purpose="diagnosis", classes=["topology", "goals"], max_depth=1,
                    relationship_types=["has_interface"])
    assert authorize(both, _authority(entities=("olt1",))) == "outside authority scope: class"


def test_the_refusal_strings_are_pinned():
    assert (REFUSED_ENTITY, REFUSED_PURPOSE, REFUSED_CLASS) == (
        "outside authority scope: entity", "outside authority scope: purpose", "outside authority scope: class")


def test_authorize_reports_entity_first_then_purpose_then_class():
    everything_wrong = _request(scope={"device": "Router-B"}, purpose="reporting")
    assert authorize(everything_wrong, _authority(classes=("goals",))) == REFUSED_ENTITY
    purpose_and_class = _request(purpose="reporting")
    assert authorize(purpose_and_class, _authority(classes=("goals",))) == REFUSED_PURPOSE


def test_the_wildcard_entity_covers_any_device():
    assert authorize(_request(scope={"device": "anything/at-all"}), _authority(entities=("*",))) is None


def test_authorize_checks_every_requested_class():
    request = _request(GOALS_BODY, classes=["goals", "observations"])
    assert authorize(request, _authority(entities=("olt1",), purposes=("assurance",),
                                         classes=("goals", "observations"))) is None
    assert authorize(request, _authority(entities=("olt1",), purposes=("assurance",),
                                         classes=("goals",))) == REFUSED_CLASS


# ---- situation identity ----

T0 = datetime(2026, 11, 5, 12, 0, 7, tzinfo=timezone.utc)

IDENTITY = dict(requester="alice", purpose="diagnosis", device="Router-A", interface=None,
                intent_id=None, valid_at=T0, known_at=None, classes=["topology"], max_depth=2,
                relationship_types=["connected_to", "has_interface"])


def _id(**changes):
    return situation_id(**{**IDENTITY, **changes})


def test_the_identity_has_a_fixed_shape():
    value = _id()
    assert value.startswith("sit-") and len(value) == 4 + 32
    int(value[4:], 16)


def test_the_same_inputs_give_the_same_identity():
    assert _id() == _id()


def test_the_identity_matches_the_pinned_known_answer():
    # PINNED: Phase 4 stores situation ids, so changing the hash inputs or the construction breaks
    # matching of stored situations. A change needs a SITUATION_VERSION bump, not an edit of this literal.
    # The literal was computed from the hand-written canonical JSON, not from situation_id itself.
    assert _id() == "sit-178907b20344e21c79bdc9c57b918ef7"


@pytest.mark.parametrize("changes", [
    {"requester": "bob"},
    {"purpose": "reporting"},
    {"device": "Router-B"},
    {"interface": "ge0"},
    {"intent_id": "hsi-demo-1"},
    {"valid_at": T0 + timedelta(seconds=1)},
    {"known_at": T0 - timedelta(hours=1)},
    {"classes": ["topology", "goals"]},
    {"max_depth": 3},
    {"max_depth": None},
    {"relationship_types": ["has_interface"]},
    {"relationship_types": None},
])
def test_each_input_changes_the_identity(changes):
    assert _id(**changes) != _id()


def test_known_none_differs_from_a_known_instant():
    assert _id(known_at=None) != _id(known_at=T0)


def test_microseconds_do_not_change_the_identity():
    first = _id(valid_at=T0.replace(microsecond=1))
    last = _id(valid_at=T0.replace(microsecond=999999))
    assert first == last == _id()
    assert _id(known_at=T0.replace(microsecond=5)) == _id(known_at=T0.replace(microsecond=999999))


def test_an_offset_timezone_is_normalised_to_utc():
    plus_two = T0.astimezone(timezone(timedelta(hours=2)))
    assert _id(valid_at=plus_two) == _id()


def test_class_and_relationship_order_do_not_matter():
    assert _id(classes=["goals", "topology"]) == _id(classes=["topology", "goals"])
    assert _id(relationship_types=["has_interface", "connected_to"]) == _id()


def test_a_device_interface_split_never_collides():
    assert _id(device="R/x", interface="y") != _id(device="R", interface="x/y")
    assert _id(device="R/x", interface=None) != _id(device="R", interface="x")


def test_a_naive_datetime_is_refused():
    with pytest.raises(ValueError):
        _id(valid_at=datetime(2026, 11, 5, 12, 0, 7))
    with pytest.raises(ValueError):
        _id(known_at=datetime(2026, 11, 5, 12, 0, 7))


def test_the_version_is_one():
    assert cc.SITUATION_VERSION == 1


# ---- wire neutrality ----

def test_the_contract_module_loads_no_web_framework():
    code = ("import sys, data_contracts.context_contract; "
            "print(any(m.split('.')[0] in ('fastapi', 'starlette') for m in sys.modules))")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(REPO))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
