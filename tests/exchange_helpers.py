"""Hand-built responses for the exchange profile tests: fixed times, demo names and fixed strings only.

Import this module as tests.exchange_helpers. Nothing here reads a clock, a file or a service, so a
fixture generated from it is a known answer (D-54).
"""
from data_contracts import context_contract as cc

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
