"""The logical context contract: request model, authority check, situation identity, times, expiry and
the response models (CTX-01, CTX-02, CTX-03, CTX-05, and the structures for CTX-06 and CTX-07).

No test here touches the network, a clock or FastAPI: the contract module is pure (D-06).
"""
import hashlib
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
EVENTS_BODY = {"purpose": "diagnosis", "scope": {"device": "Router-A"}, "classes": ["events"]}


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
    "unknown class": _body(TOPOLOGY_BODY, classes=["knowledge"]),
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
    "window without events": _body(TOPOLOGY_BODY, window_seconds=60),
    "window with goals only": _body(GOALS_BODY, window_seconds=60),
    "window zero": _body(EVENTS_BODY, window_seconds=0),
    "window negative": _body(EVENTS_BODY, window_seconds=-1),
    "window above the model maximum": _body(EVENTS_BODY, window_seconds=604801),
    "window float": _body(EVENTS_BODY, window_seconds=1.5),
    "window text": _body(EVENTS_BODY, window_seconds="60"),
    "window bool": _body(EVENTS_BODY, window_seconds=True),
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


EVENT_IDENTITY = dict(requester="alice", purpose="diagnosis", device="Router-A", interface=None,
                      intent_id=None, valid_at=T0, known_at=None, classes=["events"], max_depth=None,
                      relationship_types=None, window_seconds=900)


def test_the_identity_of_an_events_request_matches_its_pinned_known_answer():
    # PINNED (D-33): the key "window" exists only for an events request. The literal is the sha256 of
    # this hand-written canonical JSON, not a call of situation_id.
    text = ('{"classes":["events"],"intent":null,"known":null,"max_depth":null,"purpose":"diagnosis",'
            '"relationship_types":null,"requester":"alice","scope":["Router-A",null],"v":1,'
            '"valid":"2026-11-05T12:00:07Z","window":900}')
    assert "sit-" + hashlib.sha256(text.encode("ascii")).hexdigest()[:32] == (
        "sit-cbb90b643cb87083175cc9640008817a")
    assert situation_id(**EVENT_IDENTITY) == "sit-cbb90b643cb87083175cc9640008817a"


def test_two_windows_are_two_situations_and_no_window_adds_no_key():
    assert situation_id(**EVENT_IDENTITY) != situation_id(**{**EVENT_IDENTITY, "window_seconds": 60})
    # An absent window leaves every earlier id as it was (the pinned known answer above covers the bytes).
    assert situation_id(**{**IDENTITY, "window_seconds": None}) == _id()
    assert situation_id(**{**IDENTITY, "window_seconds": 900}) != _id()


def test_the_window_maximum_equals_the_store_maximum():
    from broker_state import process
    assert cc.WINDOW_SECONDS_MAX == process.MAX_EVENT_WINDOW_S == 604800


def test_the_version_is_one():
    assert cc.SITUATION_VERSION == 1


# ---- wire neutrality ----

def test_the_contract_module_loads_no_web_framework():
    code = ("import sys, data_contracts.context_contract; "
            "print(any(m.split('.')[0] in ('fastapi', 'starlette') for m in sys.modules))")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(REPO))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


# ---- freshness and time resolution ----

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


def test_freshness_seconds_accepts_a_limit_up_to_the_default():
    from broker_engine.validation import freshness_seconds
    assert freshness_seconds("2m") == 120
    assert freshness_seconds("300s") == 300
    assert freshness_seconds("5m") == 300
    assert freshness_seconds("1s") == 1


@pytest.mark.parametrize("value", ["301s", "6m", "1h", "0s", "0m", "2x", " 2m", "2m ", "2m\n", "", "m5", 2, None, 2.0])
def test_freshness_seconds_refuses_a_value_without_echoing_it(value):
    from broker_engine.validation import ClientInputError, freshness_seconds
    with pytest.raises(ClientInputError) as refused:
        freshness_seconds(value)
    message = str(refused.value)
    assert "freshness" in message and "300" in message
    if isinstance(value, str) and value.strip():
        assert value not in message


def test_freshness_seconds_names_the_label_it_is_given():
    from broker_engine.validation import ClientInputError, freshness_seconds
    with pytest.raises(ClientInputError, match="max age"):
        freshness_seconds("1h", label="max age")


def test_freshness_seconds_limit_is_the_envelope_default():
    from broker_engine.validation import freshness_seconds
    from data_contracts.envelope import DEFAULT_MAX_AGE_S
    assert freshness_seconds(f"{DEFAULT_MAX_AGE_S}s") == DEFAULT_MAX_AGE_S
    with pytest.raises(ValueError):
        freshness_seconds(f"{DEFAULT_MAX_AGE_S + 1}s")


@pytest.fixture
def default_retention(monkeypatch):
    monkeypatch.delenv("BROKER_RETENTION_PERIOD", raising=False)


def test_no_offsets_resolve_to_now_and_the_default_limit(default_retention):
    times = cc.resolve_times(_request(), NOW)
    assert times == cc.ResolvedTimes(valid_at=NOW, known_at=None, limit_s=300, historical=False)


def test_as_of_resolves_to_an_earlier_valid_instant(default_retention):
    times = cc.resolve_times(_request(as_of="10m"), NOW)
    assert times.valid_at == NOW - timedelta(minutes=10)
    assert times.known_at is None and times.historical is True


def test_known_at_alone_is_historical_with_valid_now(default_retention):
    times = cc.resolve_times(_request(known_at="1h"), NOW)
    assert times.valid_at == NOW
    assert times.known_at == NOW - timedelta(hours=1)
    assert times.historical is True


def test_freshness_can_only_tighten_the_limit(default_retention):
    assert cc.resolve_times(_request(freshness="1m"), NOW).limit_s == 60
    assert cc.resolve_times(_request(freshness="5m"), NOW).limit_s == 300


def test_freshness_above_the_default_is_a_client_input_error(default_retention):
    from broker_engine.validation import ClientInputError
    with pytest.raises(ClientInputError) as refused:
        cc.resolve_times(_request(freshness="10m"), NOW)
    assert "10m" not in str(refused.value)


def test_an_offset_beyond_retention_is_a_client_input_error(default_retention):
    from broker_engine.validation import ClientInputError
    with pytest.raises(ClientInputError, match="^as_of is further back than the retention period$"):
        cc.resolve_times(_request(as_of="31d"), NOW)
    with pytest.raises(ClientInputError, match="^known_at is further back than the retention period$"):
        cc.resolve_times(_request(known_at="31d"), NOW)


def test_the_retention_limit_follows_the_environment(monkeypatch):
    from broker_engine.validation import ClientInputError
    monkeypatch.setenv("BROKER_RETENTION_PERIOD", "2d")
    assert cc.resolve_times(_request(as_of="2d"), NOW).valid_at == NOW - timedelta(days=2)
    with pytest.raises(ClientInputError):
        cc.resolve_times(_request(as_of="3d"), NOW)


def test_a_naive_now_is_refused(default_retention):
    with pytest.raises(ValueError):
        cc.resolve_times(_request(), datetime(2026, 11, 5, 12, 0, 0))


def test_resolved_times_are_frozen():
    times = cc.ResolvedTimes(valid_at=NOW, known_at=None, limit_s=300, historical=False)
    with pytest.raises(Exception):
        times.limit_s = 1


# ---- expiry ----

R = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


def _expiry(**changes):
    args = dict(retrieved_at=R, observation_times=[], configured_evidence=False, limit_s=300, historical=False)
    return cc.expiry(**{**args, **changes})


def test_the_expiry_basis_strings_are_pinned():
    assert (cc.EXPIRY_HISTORICAL, cc.EXPIRY_OBSERVATION, cc.EXPIRY_CONFIGURED, cc.EXPIRY_NO_EVIDENCE,
            cc.EXPIRY_EVENT_WINDOW) == (
        "historical_request", "oldest_observation", "configured_state", "no_evidence", "event_window")
    assert set(cc._EXPIRY_BASES) == {
        "historical_request", "oldest_observation", "event_window", "configured_state", "no_evidence"}


def test_event_evidence_expires_a_limit_after_retrieval_and_ranks_between_observations_and_configured():
    assert _expiry(event_evidence=True, limit_s=120) == (R + timedelta(seconds=120), "event_window")
    assert _expiry(event_evidence=True, configured_evidence=True) == (R + timedelta(seconds=300), "event_window")
    assert _expiry(event_evidence=True, historical=True) == (R, "historical_request")
    result = _expiry(event_evidence=True, observation_times=[R - timedelta(seconds=10)])
    assert result == (R + timedelta(seconds=290), "oldest_observation")
    assert _expiry(event_evidence=False) == (R, "no_evidence")


def test_a_historical_request_expires_at_retrieval_whatever_else_is_passed():
    result = _expiry(historical=True, observation_times=[R - timedelta(seconds=10)], configured_evidence=True)
    assert result == (R, "historical_request")


def test_a_current_request_expires_at_the_oldest_observation_plus_the_limit():
    result = _expiry(observation_times=[R - timedelta(seconds=40), R - timedelta(seconds=100)])
    assert result == (R + timedelta(seconds=200), "oldest_observation")


def test_an_expiry_already_in_the_past_is_not_clamped():
    result = _expiry(observation_times=[R - timedelta(seconds=40), R - timedelta(seconds=100)], limit_s=60)
    assert result == (R - timedelta(seconds=40), "oldest_observation")


def test_observations_beat_configured_evidence():
    result = _expiry(observation_times=[R - timedelta(seconds=10)], configured_evidence=True)
    assert result == (R + timedelta(seconds=290), "oldest_observation")


def test_configured_evidence_alone_expires_a_limit_after_retrieval():
    assert _expiry(configured_evidence=True, limit_s=120) == (R + timedelta(seconds=120), "configured_state")


def test_no_evidence_expires_at_retrieval():
    assert _expiry() == (R, "no_evidence")


# ---- the response models ----

def _envelope(with_provenance=True):
    from data_contracts.envelope import ProvenanceRecord, build_observed
    observed = NOW - timedelta(seconds=20)
    envelope = build_observed("olt1:pon0", 12.5, "victoriametrics", observed, NOW)
    if with_provenance:
        envelope.provenance.append(ProvenanceRecord("victoriametrics", "interface_metrics_latency", observed, NOW))
    return envelope


def test_the_envelope_model_mirrors_the_dataclass_keys():
    wire = _envelope().to_dict()
    assert set(cc.EnvelopeOut.model_fields) - {"metric", "node"} == set(wire)
    assert set(cc.QualityOut.model_fields) == set(wire["quality"])
    assert set(cc.Provenance.model_fields) == set(wire["provenance"][0])


def test_an_envelope_with_a_provenance_record_validates():
    wire = {**_envelope().to_dict(), "metric": "LatencyMetric", "node": "olt1/pon0"}
    out = cc.EnvelopeOut.model_validate(wire)
    assert out.metric == "LatencyMetric" and out.node == "olt1/pon0"
    assert out.provenance[0].source_system == "victoriametrics"
    assert out.model_dump() == wire


def test_an_envelope_without_provenance_validates():
    wire = {**_envelope(with_provenance=False).to_dict(), "metric": "LatencyMetric", "node": "olt1/pon0"}
    assert cc.EnvelopeOut.model_validate(wire).provenance == []


def _provenance():
    return cc.Provenance(source_system="fuseki", reference="goal-fluent-1",
                         observed_at=None, retrieved_at="2026-11-05T12:00:00+00:00")


def _full_response(verdict=None):
    envelope = cc.EnvelopeOut.model_validate(
        {**_envelope().to_dict(), "metric": "LatencyMetric", "node": "olt1/pon0"})
    fluent = cc.FluentValue(
        context_class="goals", kind="goal", subject="LatencyMetric", value=25.0, in_force=True,
        valid_from="2026-11-05T11:00:00+00:00", valid_to=None, recorded_at="2026-11-05T11:00:01+00:00",
        superseded_at=None, provenance=_provenance())
    nodes = [
        cc.GraphNode(id="olt1", type="device", device="olt1", interface=None, graph_ref=None, depth=0),
        cc.GraphNode(id="olt1/pon0", type="interface", device="olt1", interface="pon0",
                     graph_ref="if-olt1-pon0", depth=1),
    ]
    edges = [cc.GraphEdge(from_="olt1", to="olt1/pon0", type="has_interface", link=None, fluent=None)]
    limits = cc.GraphLimits(requested_depth=1, max_depth=3, node_cap=50,
                            relationship_types=["has_interface"], cut_by=["depth"])
    eligibility = cc.Eligibility(
        result="gap", reasons=["no observation: LatencyMetric"],
        classes={"goals": cc.ClassEligibility(result="eligible", reasons=[]),
                 "observations": cc.ClassEligibility(result="gap", reasons=["no observation: LatencyMetric"])})
    situation = cc.Situation(
        id="sit-" + "0" * 32, requester="alice", purpose="assurance", scope=cc.Scope(device="olt1", interface="pon0"),
        intent_id="hsi-demo-1", classes=["topology", "goals", "observations"], max_depth=1,
        relationship_types=["has_interface"], valid_at="2026-11-05T12:00:00+00:00", known_at=None,
        retrieved_at="2026-11-05T12:00:00+00:00", historical=False)
    return cc.ContextResponse(
        situation=situation, eligibility=eligibility, expires_at="2026-11-05T12:04:40+00:00",
        expiry_basis="oldest_observation", fluent_values=[fluent], envelopes=[envelope],
        graph=cc.ContextGraph(nodes=nodes, edges=edges, limits=limits), verdict=verdict)


def test_edges_and_fluents_dump_their_aliases():
    edge = cc.GraphEdge(from_="Router-A", to="Router-A/GigabitEthernet0/0/0", type="has_interface",
                        link=None, fluent=None)
    dumped = edge.model_dump(by_alias=True)
    assert dumped["from"] == "Router-A" and "from_" not in dumped
    assert _full_response().fluent_values[0].model_dump(by_alias=True)["class"] == "goals"


def test_the_edge_accepts_its_wire_name():
    edge = cc.GraphEdge.model_validate({"from": "a", "to": "b", "type": "connected_to",
                                        "link": "link-1", "fluent": "lf-1"})
    assert edge.from_ == "a" and edge.link == "link-1"


def test_a_full_response_round_trips_unchanged():
    response = _full_response()
    text = response.model_dump_json(by_alias=True)
    assert cc.ContextResponse.model_validate_json(text) == response
    for key in ("@context", "@id", "@type"):
        assert key not in text


def test_a_response_with_a_verdict_and_no_graph_round_trips():
    verdict = {"verdict": "inconclusive", "reasons": ["goal changed: LatencyMetric"]}
    response = _full_response(verdict=verdict).model_copy(update={"graph": None})
    text = response.model_dump_json(by_alias=True)
    again = cc.ContextResponse.model_validate_json(text)
    assert again == response and again.graph is None and again.verdict == verdict


def test_the_response_json_carries_the_expected_top_level_keys():
    dumped = _full_response().model_dump(by_alias=True)
    assert set(dumped) == {"situation", "eligibility", "expires_at", "expiry_basis", "fluent_values",
                           "envelopes", "graph", "events", "verdict"}
    assert set(dumped["situation"]) == {"id", "requester", "purpose", "scope", "intent_id", "classes",
                                        "max_depth", "relationship_types", "valid_at", "known_at",
                                        "retrieved_at", "historical", "window_seconds"}
    assert set(dumped["graph"]) == {"nodes", "edges", "limits"}
    assert set(dumped["graph"]["limits"]) == {"requested_depth", "max_depth", "node_cap",
                                              "relationship_types", "cut_by"}
    assert set(dumped["fluent_values"][0]) == {"class", "kind", "subject", "value", "in_force", "valid_from",
                                               "valid_to", "recorded_at", "superseded_at", "provenance"}
    assert set(dumped["graph"]["edges"][0]) == {"from", "to", "type", "link", "fluent"}
    assert set(dumped["graph"]["nodes"][0]) == {"id", "type", "device", "interface", "graph_ref", "depth"}


def test_closed_sets_are_enforced_in_the_response_models():
    with pytest.raises(ValidationError):
        cc.GraphLimits(requested_depth=1, max_depth=3, node_cap=50, relationship_types=[], cut_by=["speed"])
    with pytest.raises(ValidationError):
        cc.ClassEligibility(result="maybe", reasons=[])
    with pytest.raises(ValidationError):
        cc.Eligibility(result="maybe", classes={}, reasons=[])
    with pytest.raises(ValidationError):
        cc.GraphNode(id="x", type="link", device="x", interface=None, graph_ref=None, depth=0)
    with pytest.raises(ValidationError):
        cc.GraphEdge(from_="a", to="b", type="peers", link=None, fluent=None)
    with pytest.raises(ValidationError):
        cc.FluentValue(context_class="goals", kind="bound", subject="x", value=1.0, in_force=True,
                       valid_from=None, valid_to=None, recorded_at=None, superseded_at=None,
                       provenance=_provenance())
    with pytest.raises(ValidationError):
        cc.ContextResponse.model_validate({**_full_response().model_dump(by_alias=True), "expiry_basis": "whenever"})


def test_every_cut_reason_is_accepted():
    limits = cc.GraphLimits(requested_depth=1, max_depth=3, node_cap=50, relationship_types=[],
                            cut_by=list(cc.CUT_REASONS))
    assert limits.cut_by == list(cc.CUT_REASONS)


def test_unknown_response_fields_are_refused():
    with pytest.raises(ValidationError):
        cc.QualityOut(stale=False, missing=False, conflicts=[], extra=1)


def test_eligibility_is_described_as_kept_apart_from_the_verdict():
    description = cc.Eligibility.model_json_schema()["description"]
    assert "no execution authority" in description
    assert "verdict" in description
    verdict = cc.ContextResponse.model_json_schema()["properties"]["verdict"]["description"]
    assert "read-only TMF921 assess" in verdict
    assert "never reads eligibility" in verdict
    assert "can disagree" in verdict


def test_the_response_schema_never_says_bound_or_gate():
    text = _schema_text()
    lowered = text.lower()
    assert " bound" not in lowered and "drift" not in lowered and " gate" not in lowered


def _schema_text():
    import json
    return json.dumps([cc.ContextResponse.model_json_schema(), cc.ContextRequest.model_json_schema()])


def test_the_module_still_loads_no_web_framework_after_the_response_models():
    code = ("import sys, data_contracts.context_contract as c; "
            "print(any(m.split('.')[0] in ('fastapi', 'starlette') for m in sys.modules), "
            "hasattr(c, 'ContextResponse'))")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(REPO))
    assert result.stdout.strip() == "False True", result.stderr


# ---- the events result and the window ----

def test_a_situation_stored_before_the_window_field_still_validates():
    body = _full_response().situation.model_dump()
    body.pop("window_seconds")
    assert cc.Situation.model_validate(body).window_seconds is None


def test_an_events_result_round_trips_and_forbids_extra_keys():
    event = cc.EventOut(
        event_id="evt-" + "a" * 32, mapping="mapped", device="Router-A", source_host="Router-A",
        candidates=[], candidates_withheld=0, format="rfc5424", facility=3, severity=4,
        event_time=None, ingest_time="2026-11-05T12:00:00+00:00", kind=None, flags=["time_unknown"],
        provenance=cc.Provenance(source_system="syslog", reference="evt-" + "a" * 32, observed_at=None,
                                 retrieved_at="2026-11-05T12:00:00+00:00"))
    result = cc.EventsResult(
        window=cc.EventWindow(start="2026-11-05T11:45:00+00:00", end="2026-11-05T12:00:00+00:00", seconds=900),
        cap=100, truncated=False, events=[event])
    assert cc.EventsResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        cc.EventOut.model_validate({**event.model_dump(), "message": "x"})
    with pytest.raises(ValidationError):
        cc.EventOut.model_validate({**event.model_dump(), "flags": ["odd"]})
    assert cc.EVENT_FLAGS == ("ambiguous", "future_dated", "time_unknown")
    assert cc.EVENT_RESULT_CAP == 100 and cc.EVENT_SOURCE == "syslog"


def test_an_events_body_with_a_window_parses():
    request = ContextRequest.model_validate({**EVENTS_BODY, "window_seconds": 60})
    assert request.window_seconds == 60
    assert ContextRequest.model_validate(EVENTS_BODY).window_seconds is None
