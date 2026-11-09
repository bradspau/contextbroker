"""Hand-built responses for the exchange profile tests: fixed times, demo names and fixed strings only.

Import this module as tests.exchange_helpers. Nothing here reads a clock, a file or a service, so a
fixture generated from it is a known answer (D-54).
"""
import json
from pathlib import Path

from data_contracts import context_contract as cc
from data_contracts import exchange_profile as ep
from data_contracts import promotion as pr

T0 = "2026-11-06T12:00:00+00:00"
T_RECORDED = "2026-11-06T11:00:01+00:00"
T_EXPIRES = "2026-11-06T12:05:00+00:00"

LINK = "link-router-a-router-b"
LINK_FLUENT = "link-router-a-router-b-fluent-demo"
IF_A = "Router-A/GigabitEthernet0/0/0"
IF_B = "Router-B/GigabitEthernet0/0/1"
SITUATION_ID = "sit-" + "0123456789abcdef" * 2


def _fuseki(reference: str) -> cc.Provenance:
    return cc.Provenance(source_system="fuseki", reference=reference, observed_at=None, retrieved_at=T0)


def topology_response() -> cc.ContextResponse:
    """A topology-only answer: four nodes, two has_interface edges, one connected_to edge, one link fluent."""
    nodes = [
        cc.GraphNode(id="Router-A", type="device", device="Router-A", interface=None, graph_ref=None, depth=0),
        cc.GraphNode(id=IF_A, type="interface", device="Router-A", interface="GigabitEthernet0/0/0",
                     graph_ref="if-router-a-gi0-0-0", depth=1),
        cc.GraphNode(id=IF_B, type="interface", device="Router-B", interface="GigabitEthernet0/0/1",
                     graph_ref="if-router-b-gi0-0-1", depth=2),
        cc.GraphNode(id="Router-B", type="device", device="Router-B", interface=None, graph_ref=None, depth=3),
    ]
    edges = [
        cc.GraphEdge(from_="Router-A", to=IF_A, type="has_interface", link=None, fluent=None),
        cc.GraphEdge(from_=IF_A, to=IF_B, type="connected_to", link=LINK, fluent=LINK_FLUENT),
        cc.GraphEdge(from_="Router-B", to=IF_B, type="has_interface", link=None, fluent=None),
    ]
    limits = cc.GraphLimits(requested_depth=3, max_depth=3, node_cap=50,
                            relationship_types=["has_interface", "connected_to"], cut_by=[])
    link = cc.FluentValue(
        context_class="topology", kind="link", subject=LINK, value=[IF_A, IF_B], in_force=True,
        valid_from="2026-11-01T00:00:00+00:00", valid_to=None, recorded_at=T_RECORDED, superseded_at=None,
        provenance=_fuseki(LINK_FLUENT))
    eligibility = cc.Eligibility(
        result="eligible", reasons=[],
        classes={"topology": cc.ClassEligibility(result="eligible", reasons=[])})
    situation = cc.Situation(
        id=SITUATION_ID, requester="demo-caller", purpose="assurance",
        scope=cc.Scope(device="Router-A", interface=None), intent_id=None, classes=["topology"], max_depth=3,
        relationship_types=["has_interface", "connected_to"], valid_at=T0, known_at=None, retrieved_at=T0,
        historical=False)
    return cc.ContextResponse(
        situation=situation, eligibility=eligibility, expires_at=T_EXPIRES, expiry_basis="configured_state",
        fluent_values=[link], envelopes=[],
        graph=cc.ContextGraph(nodes=nodes, edges=edges, limits=limits), verdict=None)


T_OBSERVED = "2026-11-06T11:59:30+00:00"
T_EVENT = "2026-11-06T11:40:00+00:00"
T_PROMOTED = "2026-11-06T11:55:00+00:00"
EVENT_1 = "evt-" + "1" * 32
EVENT_2 = "evt-" + "2" * 32
EVENT_3 = "evt-" + "3" * 32
HYPOTHESIS_ENDS = ("Router-A/GigabitEthernet0/0/1", "Router-C/GigabitEthernet0/0/2")
CONFLICT_TEXT = "event link_down contradicts configured link_in_force"
VERDICT = {"verdict": "inconclusive", "reasons": ["goal changed: LatencyMetric"]}


def hypothesis_edge() -> cc.HypothesisEdge:
    pid = pr.promoted_id("hypothesis", pr.RULE_ADJACENCY, HYPOTHESIS_ENDS)
    return cc.HypothesisEdge(
        promoted_id=pid, from_=HYPOTHESIS_ENDS[0], to=HYPOTHESIS_ENDS[1], label="hypothesis", state="inferred",
        rule=pr.RULE_ADJACENCY, valid_from=T_EVENT, promoted_at=T_PROMOTED,
        support=[cc.SupportRef(kind="situation", id=None, state="withheld"),
                 cc.SupportRef(kind="event", id=EVENT_2, state="present"),
                 cc.SupportRef(kind="event", id=EVENT_3, state="present")],
        provenance=cc.Provenance(source_system=pr.PROMOTION_SOURCE, reference=pid, observed_at=None,
                                 retrieved_at=T0))


def promoted_conflict() -> cc.PromotedConflictOut:
    pid = pr.promoted_id("conflict", pr.RULE_REPEATED_LINK_DOWN, ("Router-A",))
    return cc.PromotedConflictOut(
        promoted_id=pid, rule=pr.RULE_REPEATED_LINK_DOWN, device="Router-A", event="link_down",
        configured="link_in_force", text=CONFLICT_TEXT, valid_from=T_EVENT, promoted_at=T_PROMOTED,
        support=[cc.SupportRef(kind="situation", id=SITUATION_ID, state="present"),
                 cc.SupportRef(kind="event", id=EVENT_1, state="evicted")],
        provenance=cc.Provenance(source_system=pr.PROMOTION_SOURCE, reference=pid, observed_at=None,
                                 retrieved_at=T0))


def _events_result() -> cc.EventsResult:
    quality = cc.QualityOut(stale=False, missing=False, conflicts=[CONFLICT_TEXT])
    provenance = cc.Provenance(source_system="syslog", reference=EVENT_1, observed_at=T_EVENT, retrieved_at=T0)
    event = cc.EventOut(
        event_id=EVENT_1, mapping="mapped", device="Router-A", source_host="Router-A", candidates=[],
        candidates_withheld=0, format="rfc5424", facility=23, severity=3, event_time=T_EVENT,
        ingest_time="2026-11-06T11:40:02+00:00", kind="link_down", flags=[], provenance=provenance)
    envelope = cc.EventEnvelopeOut(
        event_id=EVENT_1, node="Router-A", entity_id="Router-A", state="observed", value="link_down",
        source="syslog", observed_at=T_EVENT, quality=quality, provenance=[provenance])
    return cc.EventsResult(
        window=cc.EventWindow(start="2026-11-06T11:00:00+00:00", end=T0, seconds=3600), cap=100,
        truncated=False, events=[event], envelopes=[envelope],
        configured=cc.ConfiguredOut(device="Router-A", status="held", value="link_in_force", fluents=[LINK],
                                    authority_cut=False),
        conflicts=[cc.ConflictOut(event_id=EVENT_1, device="Router-A", event="link_down",
                                  configured="link_in_force", text=CONFLICT_TEXT)],
        promoted_conflicts=[promoted_conflict()])


def _knowledge_result() -> cc.KnowledgeResult:
    reference = cc.KnowledgeRef(
        identifier="runbook-demo", version="1.0", status="approved", title="Demo runbook",
        content_hash="0" * 64,
        matched_scope=cc.MatchedScope(entities=["Router-A"], purpose="assurance", ontology_class="Device"),
        source_references=["demo-source"], retrieval_rank=1,
        linked_actions=[cc.ActionRef(template_id="restart-demo", version="1", approved=True)],
        unresolved_actions=0,
        provenance=cc.Provenance(source_system="okf", reference="runbook-demo@1.0", observed_at=None,
                                 retrieved_at=T0))
    return cc.KnowledgeResult(retrieval="answered", cap=10, truncated=False, references=[reference], withheld=0)


def full_response() -> cc.ContextResponse:
    """All five classes, a verdict, a promoted conflict and a hypothesis edge."""
    base = topology_response()
    goal = cc.FluentValue(
        context_class="goals", kind="goal", subject="LatencyMetric", value=25.0, in_force=True,
        valid_from="2026-11-05T11:00:00+00:00", valid_to=None, recorded_at=T_RECORDED, superseded_at=None,
        provenance=_fuseki("goal-fluent-demo"))
    placement = cc.FluentValue(
        context_class="goals", kind="placement", subject="LatencyMetric", value=IF_A, in_force=True,
        valid_from="2026-11-05T11:00:00+00:00", valid_to=None, recorded_at=T_RECORDED, superseded_at=None,
        provenance=_fuseki("placement-fluent-demo"))
    envelope = cc.EnvelopeOut(
        metric="LatencyMetric", node=IF_A, entity_id="Router-A:GigabitEthernet0/0/0", state="observed",
        value=12.5, source="victoriametrics", observed_at=T_OBSERVED,
        quality=cc.QualityOut(stale=False, missing=False, conflicts=[]),
        provenance=[cc.Provenance(source_system="victoriametrics", reference="interface_metrics_latency",
                                  observed_at=T_OBSERVED, retrieved_at=T0)])
    names = ["topology", "goals", "observations", "events", "knowledge"]
    eligibility = cc.Eligibility(
        result="eligible", reasons=[],
        classes={name: cc.ClassEligibility(result="eligible", reasons=[]) for name in names})
    situation = base.situation.model_copy(update={
        "intent_id": "hsi-demo-1", "classes": names, "window_seconds": 3600, "topic": "link flap"})
    graph = base.graph.model_copy(update={"hypotheses": [hypothesis_edge()]})
    return cc.ContextResponse(
        situation=situation, eligibility=eligibility, expires_at="2026-11-06T12:04:30+00:00",
        expiry_basis="oldest_observation", fluent_values=[*base.fluent_values, goal, placement],
        envelopes=[envelope], graph=graph, events=_events_result(), knowledge=_knowledge_result(),
        verdict=dict(VERDICT))


def write_fixtures(directory: Path) -> None:
    """Write the known-answer pair: the native form and the JSON-LD form of full_response()."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    response = full_response()
    pairs = (("response-native.json", response.model_dump(mode="json", by_alias=True)),
             ("response.jsonld", ep.to_jsonld(response)))
    for name, document in pairs:
        (directory / name).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
