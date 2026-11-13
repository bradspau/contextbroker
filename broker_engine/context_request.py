"""The I/O shell of the context request (CTX-01 to CTX-08).

The events class reads the state file, is judged on its own and never returns device-authored text
(no raw line, no message): only structured fields, a recognised kind and quality flags. When a
recognised event on a mapped device is returned it also reads the device's link fluents in Fuseki
(SELECT only) and publishes a contradiction as a conflict naming both sides; the configured side is
never written and the conflict never reaches the eligibility or the verdict.

Promoted records (a conflict an operator promoted for the scope device, and a topology link inferred from
stored neighbour-discovery events) are read from the state file only, in the events and topology classes. They
are delivered with what they rest on and never written by a request. A promoted record never counts as
evidence: it feeds neither the eligibility, the expiry nor the verdict, and a hypothesis is never a
configured fact (it sits beside the configured edges, labelled and stated as inferred).

The knowledge class reads reviewed files, ranked by a retrieval index (the index orders, the files decide
which apply), and is judged on its own. It returns references only: the identifier, version, status,
title, content hash, matched scope, source references and the approved actions by id and version, never
the body of an artefact or a ranking value. It never executes or schedules an action, and it never feeds
the verdict or the eligibility of another class.

It composes the pure modules (the request contract, eligibility, the Context Graph, the binding
selection) over the sources: one Fuseki read for the intent's fluents, the Context Graph reads, one
VictoriaMetrics read per placed metric, and for the events class one read of the state file. It
resolves, joins and delivers context and never decides or acts: it pushes nothing to the TMF921
server, writes no high-water mark and records no outcome.

Every refusal (no authority claim, a request outside the caller's entities, purposes or classes, an
intent named without ``context:assess``, an offset beyond retention, a freshness above the default
limit) happens before the first Fuseki, VictoriaMetrics or TMF921 call (D-10). Nothing here logs the
request body, the requester or a claim value.
"""
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

import requests
from pydantic import ValidationError

from broker_engine import context_graph
from broker_engine.auth import SCOPE_ASSESS, Caller
from broker_engine.binding_resolver import fetch_fluents, select_binding
from broker_engine.context_eligibility import (
    FUTURE_TOLERANCE_S,
    INVALID_BINDING,
    NO_BINDING,
    binding_problem,
    events_eligibility,
    goals_eligibility,
    knowledge_eligibility,
    observations_eligibility,
    overall,
    overlapping,
    topology_eligibility,
)
from broker_engine.faults import log_fault
from broker_engine.sparql_resolver import FUSEKI_QUERY_ENDPOINT
from broker_engine.telemetry_reads import read_in_order
from broker_engine.temporal import temporal_observation
from broker_engine.tsdb_joiner import join_observed_metric
from broker_engine.validation import ClientInputError
from data_contracts.context_contract import (
    CONTEXT_CLASSES,
    EVENT_RESULT_CAP,
    EVENT_SOURCE,
    ContextRequest,
    ConfiguredOut,
    ActionRef,
    ConflictOut,
    ContextResponse,
    EnvelopeOut,
    EventEnvelopeOut,
    EventOut,
    EventsResult,
    EventWindow,
    FluentValue,
    HYPOTHESIS_RESULT_CAP,
    HypothesisEdge,
    KnowledgeRef,
    KnowledgeResult,
    MatchedScope,
    PromotedConflictOut,
    Provenance,
    Situation,
    SupportRef,
    authorize,
    authorize_scope,
    expiry,
    resolve_times,
    situation_id,
)
from broker_state.process import event_window_limits, get_store
from data_contracts.knowledge import KNOWLEDGE_RESULT_CAP, KNOWLEDGE_SOURCE
from data_contracts.metric_map import load_metric_map
from data_contracts.promotion import HYPOTHESIS_LABEL, HYPOTHESIS_STATE, PROMOTION_SOURCE
from data_contracts.syslog_events import EVENT_KINDS
from data_contracts.situation_record import (
    SITUATION_ID_PATTERN,
    SituationRecord,
    StoredSituation,
    stored_situation,
)
from intent_management.tmf921_source import assess_tmf921_as_of
from okf_retrieval.service import ApplicableRef, get_service
from outcome_assurance.tmf921_report import short_name
from state_reconciliation.event_reconciler import (
    CONFIGURED_LINK_IN_FORCE,
    ConfiguredSide,
    EventFact,
    conflict_text,
    reconcile,
)

# The telemetry lookback is twice the default freshness limit, so a sample that is too old is read and
# reported stale (ineligible) instead of vanishing from the window and reading as missing (a gap).
OBSERVATION_LOOKBACK = "10m"

log = logging.getLogger(__name__)

# The one line written for a stored promoted record that does not build its model: no id, device or text.
PROMOTED_UNREADABLE = "promoted record unreadable"
# One promoted conflict exists per rule and device (the id is built from both), so a few cover a device.
PROMOTED_CONFLICT_LIMIT = 10

AUTHORITY_REQUIRED = "authority claims required"
INSUFFICIENT_SCOPE = "insufficient scope"
SITUATION_NOT_FOUND = "situation not found"
WINDOW_ABOVE_MAX = "window_seconds above the event window maximum"


# A sending host is returned only when it is a plain token like a device name (research Pitfall 8).
_HOST_TOKEN = re.compile(r"[A-Za-z0-9_\-/]{1,128}")


class Refused(Exception):
    """A request the caller may not make. The message is always one of the fixed details."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def recording(join, seen):
    """Wrap a joiner so each distinct read happens once and its envelope is kept in `seen`.

    The key is (device, interface, metric, window, aggregate, as_of); a repeated call returns the
    envelope already read, so eligibility and a later verdict share one telemetry read.

    Safe for callers on several threads (the metrics of a request are read together). A lock guards
    only the checks and the store, never the read itself. The first caller for a key reads; a
    concurrent caller for the same key waits for that read and gets the stored envelope, so every
    caller holds one object. A read that raises stores nothing and the waiters try again."""
    lock = threading.Lock()
    pending = {}  # key -> the event its reading caller sets when the read is over

    def wrapped(device, interface, metric, window, **kw):
        key = (device, interface, metric, window, kw.get("aggregate"), kw.get("as_of"))
        while True:
            with lock:
                if key in seen:
                    return seen[key]
                done = pending.get(key)
                if done is None:
                    done = pending[key] = threading.Event()
                    mine = True
                else:
                    mine = False
            if not mine:
                done.wait()
                continue
            try:
                envelope = join(device, interface, metric, window, **kw)
                with lock:
                    seen[key] = envelope
                return envelope
            finally:
                with lock:
                    pending.pop(key, None)
                done.set()
    return wrapped


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(timezone.utc).isoformat()


def _event_out(row, retrieved_at: datetime, covers) -> EventOut:
    """One stored event as the response shows it: structured fields, a recognised kind and flags. The
    raw line is never read here. An ambiguous event names only the candidates the caller's entities
    cover and counts the rest (research Pitfall 7)."""
    flags = []
    if row.event_time is None:
        flags.append("time_unknown")
    elif (row.event_time - row.ingest_time).total_seconds() > FUTURE_TOLERANCE_S:
        flags.append("future_dated")
    candidates, withheld = [], 0
    if row.mapping == "ambiguous":
        flags.append("ambiguous")
        candidates = sorted(c for c in row.candidates if covers(c))
        withheld = len(row.candidates) - len(candidates)
    return EventOut(
        event_id=row.event_id, mapping=row.mapping, device=row.device,
        source_host=row.source_host if _HOST_TOKEN.fullmatch(row.source_host) else None,
        candidates=candidates, candidates_withheld=withheld, format=row.format,
        facility=row.facility, severity=row.severity, event_time=_iso(row.event_time),
        ingest_time=_iso(row.ingest_time), kind=row.kind, flags=sorted(flags),
        provenance=Provenance(source_system=EVENT_SOURCE, reference=row.event_id,
                              observed_at=_iso(row.event_time), retrieved_at=_iso(retrieved_at)))


def _knowledge_ref(ref: ApplicableRef, retrieved_at: datetime) -> KnowledgeRef:
    """One applicable bundle as a reference: identity, version, status, title, content hash, the scope
    patterns that matched, the source references, the rank and the approved actions by id and version.
    The bundle body and its embedding text are never read here (D-45, D-46)."""
    bundle = ref.bundle
    return KnowledgeRef(
        identifier=bundle.identifier, version=bundle.version, status=bundle.status, title=bundle.title,
        content_hash=bundle.content_hash,
        matched_scope=MatchedScope(entities=list(ref.match.entities), purpose=ref.match.purpose,
                                   ontology_class=ref.match.ontology_class),
        source_references=list(bundle.source_references), retrieval_rank=ref.rank,
        linked_actions=[ActionRef(template_id=a.template_id, version=a.version, approved=True)
                        for a in ref.actions],
        unresolved_actions=ref.unresolved_actions,
        provenance=Provenance(source_system=KNOWLEDGE_SOURCE, reference=f"{bundle.identifier}@{bundle.version}",
                              observed_at=None, retrieved_at=_iso(retrieved_at)))


def _promotion_provenance(row, retrieved_at: datetime) -> Provenance:
    return Provenance(source_system=PROMOTION_SOURCE, reference=row.promoted_id,
                      observed_at=_iso(row.promoted_at), retrieved_at=_iso(retrieved_at))


def _support(row, now: datetime, requester: str) -> list[SupportRef]:
    """What a promoted record rests on: the situation first, then each event in stored order. Whether each
    still exists comes from the state file at `now`; an evicted source stays listed and marked (D-60). The
    situation id goes only to the requester who asked that question; anyone else sees "withheld" and no id,
    whether or not the situation still exists (D-66)."""
    situation_present, event_present = get_store().support_present(
        row.situation_id, row.event_ids, now=now)
    if row.situation_requester == requester:
        first = SupportRef(kind="situation", id=row.situation_id,
                           state="present" if situation_present else "evicted")
    else:
        first = SupportRef(kind="situation", id=None, state="withheld")
    return [first] + [SupportRef(kind="event", id=event_id, state="present" if present else "evicted")
                      for event_id, present in zip(row.event_ids, event_present)]


def _promoted_conflicts(device: str, times, now: datetime, requester: str) -> list[PromotedConflictOut]:
    """The conflicts promoted for `device` (already authorised), newest first. Read from the state file
    only. They are delivered beside the live conflicts and never feed the eligibility, the expiry or the
    verdict (D-61, D-64). A row that does not build its model is skipped with one fixed line."""
    rows = get_store().promoted_rows(
        "conflict", device, valid_at=times.valid_at, known_at=times.known_at, now=now,
        limit=PROMOTED_CONFLICT_LIMIT)
    out = []
    for row in rows:
        try:
            body = json.loads(row.body)
            if body["device"] != row.device_a:
                raise ValueError("body names another device")
            # The sentence is rebuilt from the two closed vocabularies; the stored text is never echoed.
            out.append(PromotedConflictOut(
                promoted_id=row.promoted_id, rule=row.rule, device=row.device_a, event=body["event"],
                configured=body["configured"], text=conflict_text(body["event"], body["configured"]),
                valid_from=_iso(row.valid_from), promoted_at=_iso(row.promoted_at),
                support=_support(row, now, requester), provenance=_promotion_provenance(row, now)))
        except (ValueError, TypeError, KeyError):
            log.warning(PROMOTED_UNREADABLE)
    return out


def _hypotheses(graph, times, now: datetime, requester: str, covers
                ) -> tuple[list[HypothesisEdge], bool, int]:
    """The promoted hypotheses incident to a returned node: (edges, truncated, withheld). Read from the
    state file only, one bounded read per device in the graph.

    A hypothesis is kept when at least one end is a returned node, and is not returned when its two ends
    are already a configured connected_to edge of this graph (the status is computed here, at read time,
    and nothing is stored for it). One whose end lies on a device the caller's entities do not cover is
    counted in `withheld` and never named. The rest are sorted by (from, to, promoted_id) and cut at the
    cap. They are separate from the configured edges and add no node, limit or fluent value (D-63, D-67)."""
    node_ids = {node.id for node in graph.nodes}
    configured = {frozenset((edge.from_, edge.to)) for edge in graph.edges if edge.type == "connected_to"}
    limit = HYPOTHESIS_RESULT_CAP + 1
    found, full = {}, False
    for device in sorted({node.device for node in graph.nodes}):
        rows = get_store().promoted_rows(
            "hypothesis", device, valid_at=times.valid_at, known_at=times.known_at, now=now, limit=limit)
        full = full or len(rows) >= limit
        for row in rows:
            found.setdefault(row.promoted_id, row)
    kept, withheld = [], 0
    for row in found.values():
        try:
            ends = json.loads(row.body)
            first, second = ends["from"], ends["to"]
            if (row.device_b is None or not isinstance(first, str) or not isinstance(second, str)
                    or first.split("/", 1)[0] != row.device_a or second.split("/", 1)[0] != row.device_b):
                raise ValueError("body does not match the record's devices")
        except (ValueError, TypeError, KeyError):
            log.warning(PROMOTED_UNREADABLE)
            continue
        if first not in node_ids and second not in node_ids:
            continue
        if frozenset((first, second)) in configured:
            continue
        if not (covers(row.device_a) and covers(row.device_b)):
            withheld += 1
            continue
        kept.append((first, second, row))
    kept.sort(key=lambda item: (item[0], item[1], item[2].promoted_id))
    truncated = full or len(kept) > HYPOTHESIS_RESULT_CAP
    edges = []
    for first, second, row in kept[:HYPOTHESIS_RESULT_CAP]:
        try:
            edges.append(HypothesisEdge(
                promoted_id=row.promoted_id, from_=first, to=second, label=HYPOTHESIS_LABEL,
                state=HYPOTHESIS_STATE, rule=row.rule, valid_from=_iso(row.valid_from),
                promoted_at=_iso(row.promoted_at), support=_support(row, now, requester),
                provenance=_promotion_provenance(row, now)))
        except (ValueError, TypeError):
            log.warning(PROMOTED_UNREADABLE)
    return edges, truncated, withheld


def _fluent_value(metric, kind, fluent, device_interface, valid_at, retrieved_at) -> FluentValue:
    """One goal or placement fluent as a response value. A placement's value is its node id."""
    value = fluent.value if kind == "goal" else "/".join(device_interface)
    return FluentValue(
        context_class="goals", kind=kind, subject=short_name(metric), value=value,
        in_force=fluent.covers(valid_at), valid_from=_iso(fluent.start), valid_to=_iso(fluent.end),
        recorded_at=_iso(fluent.recorded), superseded_at=_iso(fluent.superseded),
        provenance=Provenance(source_system="fuseki", reference=fluent.iri,
                              observed_at=_iso(fluent.recorded), retrieved_at=_iso(retrieved_at)))


def handle(body: ContextRequest, caller: Caller, *, now: datetime | None = None, join=None,
           query_endpoint: str = FUSEKI_QUERY_ENDPOINT, client=None,
           metric_map=None) -> ContextResponse:
    """Answer one context request for an authenticated caller.

    `now` is read once and every time in the answer derives from it. `join`, `client` and
    `metric_map` default to the real joiner, TMF921 client and metric map."""
    authority = caller.authority
    if authority is None:
        raise Refused(AUTHORITY_REQUIRED)
    detail = authorize(body, authority)
    if detail is not None:
        raise Refused(detail)
    if body.intent_id is not None and SCOPE_ASSESS not in caller.scopes:
        raise Refused(INSUFFICIENT_SCOPE)

    now = (now if now is not None else _utcnow()).astimezone(timezone.utc)
    times = resolve_times(body, now)
    covers = authority.covers_entity
    # The event window is resolved here, before any upstream call: the default applies when none is
    # named, and a window above BROKER_EVENT_WINDOW_MAX is a fixed 400 (D-13).
    window_s = None
    if "events" in body.classes:
        default_s, max_s = event_window_limits()
        window_s = default_s if body.window_seconds is None else body.window_seconds
        if window_s > max_s:
            raise ClientInputError(WINDOW_ABOVE_MAX)

    classes = sorted(body.classes)
    relationship_types = None if body.relationship_types is None else sorted(body.relationship_types)
    identity = situation_id(
        requester=caller.requester, purpose=body.purpose, device=body.scope.device,
        interface=body.scope.interface, intent_id=body.intent_id, valid_at=times.valid_at,
        known_at=times.known_at, classes=classes, max_depth=body.max_depth,
        relationship_types=relationship_types, window_seconds=window_s, topic=body.topic)

    eligibility = {}
    fluent_values: list[FluentValue] = []
    priority: set[str] = set()
    envelopes: list[EnvelopeOut] = []
    observation_times: list[datetime] = []
    wants_observations = "observations" in body.classes
    verdict = None

    if "goals" in body.classes:
        # One Fuseki read of the intent's fluents. The same snapshot judges the goals class and picks
        # the binding the telemetry is read for.
        problem = None
        fluents, interfaces = {}, {}
        mapping = metric_map
        # What the verdict is told about the binding: None lets the assess resolve it itself (and refuse
        # with its own fixed reason), {} says the intent has none, and {intent_id: binding} is the
        # binding picked from the one snapshot below.
        verdict_bindings = None
        binding = None
        seen = {}
        reader = recording(join if join is not None else join_observed_metric, seen)
        if mapping is None:
            try:
                mapping = load_metric_map()
            except ValueError:
                problem = INVALID_BINDING
        if problem is None:
            try:
                fluents, interfaces = fetch_fluents(body.intent_id, query_endpoint)
            except requests.RequestException:
                raise
            except ValueError:
                problem = INVALID_BINDING
            else:
                if not fluents:
                    problem = NO_BINDING
                    verdict_bindings = {}
        observation_problem = problem
        if problem is not None:
            eligibility["goals"] = binding_problem(problem)
        else:
            eligibility["goals"] = goals_eligibility(
                fluents, interfaces, mapping, valid_at=times.valid_at, known_at=times.known_at,
                limit_s=times.limit_s, allowed=covers)
            window_start = times.valid_at - timedelta(seconds=times.limit_s)
            for metric, kind in sorted(fluents):
                for fluent in overlapping(fluents[(metric, kind)], window_start, times.valid_at,
                                          times.known_at):
                    where = None
                    if kind == "placement":
                        where = interfaces.get(fluent.value)
                        if where is None or not covers(where[0]):
                            continue  # a placement outside the caller's entities is not delivered
                        priority.update({where[0], "/".join(where)})
                    fluent_values.append(_fluent_value(
                        metric, kind, fluent, where, times.valid_at, now))
            try:
                binding = select_binding(body.intent_id, fluents, interfaces,
                                         valid_at=times.valid_at, known_at=times.known_at)
                verdict_bindings = {body.intent_id: binding}
            except ValueError:
                observation_problem = INVALID_BINDING

        if wants_observations:
            if observation_problem is not None:
                eligibility["observations"] = binding_problem(observation_problem)
            else:
                observed, nodes = {}, {}
                # A placement the caller's entities do not cover is never read (D-26), and a metric the
                # map does not know has no series name to read.
                # The covered, mapped metrics are read together (BROKER_TELEMETRY_WORKERS); a metric
                # whose identifier the joiner refuses comes back None and stays unobserved.
                to_read = []
                for uri in sorted(binding.bounds):
                    device, interface = binding.sources[uri]
                    mapped = mapping.get(uri)
                    if mapped is None or not covers(device):
                        continue
                    to_read.append((uri, device, interface, mapped.broker_metric))

                def read_one(item):
                    _uri, device, interface, broker_metric = item
                    try:
                        return temporal_observation(
                            reader, device, interface, {"metric": broker_metric},
                            OBSERVATION_LOOKBACK, "at", body.as_of, now)
                    except ClientInputError:
                        return None  # left unobserved: a gap

                for (uri, device, interface, _metric), envelope in zip(
                        to_read, read_in_order(read_one, to_read)):
                    if envelope is None:
                        continue
                    observed[uri] = envelope
                    nodes[uri] = f"{device}/{interface}"
                eligibility["observations"] = observations_eligibility(
                    sorted({metric for metric, _kind in fluents}), observed,
                    valid_at=times.valid_at, limit_s=times.limit_s)
                for uri, envelope in observed.items():
                    envelopes.append(EnvelopeOut(metric=short_name(uri), node=nodes[uri],
                                                 **envelope.to_dict()))
                    if envelope.observed_at is not None:
                        observation_times.append(envelope.observed_at)
                envelopes.sort(key=lambda e: e.metric)

        # The verdict (D-28): the read-only TMF921 assess, judged on the same fluent snapshot (the binding
        # picked above) and the same telemetry reads (the recording joiner) as the eligibility. It never
        # sees the eligibility and the eligibility never sees it: the two can disagree by design (D-19).
        # It is skipped when a placement lies outside the caller's entities, since judging it would read
        # a device the caller may not see (D-26).
        outside = binding is not None and any(
            not covers(device) for device, _interface in binding.sources.values())
        if wants_observations and not outside:
            verdict = assess_tmf921_as_of(
                body.intent_id, OBSERVATION_LOOKBACK, as_of=body.as_of, mode="at",
                known_at=body.known_at, client=client, join=reader, now=now, metric_map=mapping,
                bindings=verdict_bindings)
            verdict.pop("context", None)  # duplicates the envelopes
            verdict.pop("goals", None)  # duplicates the fluent values

    graph = None
    if "topology" in body.classes:
        read = context_graph.build_graph(
            body.scope.device, body.scope.interface, max_depth=body.max_depth,
            relationship_types=body.relationship_types, valid_at=times.valid_at,
            known_at=times.known_at, retrieved_at=now, allowed=covers, priority=frozenset(priority),
            query_endpoint=query_endpoint)
        graph = read.graph
        if "connected_to" in body.relationship_types:
            # Promoted hypotheses sit beside the configured edges, never among them. The eligibility below
            # is computed from `read` exactly as before and the graph's nodes, edges and limits are untouched.
            edges, hypotheses_truncated, hypotheses_withheld = _hypotheses(
                graph, times, now, caller.requester, covers)
            graph = graph.model_copy(update={
                "hypotheses": edges, "hypotheses_truncated": hypotheses_truncated,
                "hypotheses_withheld": hypotheses_withheld})
        fluent_values.extend(read.fluent_values)
        eligibility["topology"] = topology_eligibility(
            read.scope_identity, conflicting_links=read.conflicting_links,
            colliding_ids=read.colliding_ids, malformed=read.malformed)

    events = None
    if "events" in body.classes:
        # The state file only: no Fuseki, VictoriaMetrics or TMF921 call. The window ends at the
        # resolved instant; one row past the cap tells whether the cap cut the result.
        start = times.valid_at - timedelta(seconds=window_s)
        rows = get_store().events_in_window(
            body.scope.device, start=start, end=times.valid_at, known_at=times.known_at, now=now,
            limit=EVENT_RESULT_CAP + 1)
        truncated = len(rows) > EVENT_RESULT_CAP
        returned = [_event_out(row, now, covers) for row in rows[:EVENT_RESULT_CAP]]
        # Reconciliation (D-16 to D-19): only a recognised kind on a mapped event is compared, and the
        # configured side is read (SELECT only) only when there is at least one. An ambiguous or
        # unrecognised event gets no envelope and causes no read.
        recognised = [row for row in rows[:EVENT_RESULT_CAP]
                      if row.mapping == "mapped" and row.device is not None and row.kind in EVENT_KINDS]
        envelope_out, conflicts_out, configured_out = [], [], None
        if recognised:
            links = context_graph.device_links_in_force(
                body.scope.device, valid_at=times.valid_at, known_at=times.known_at, retrieved_at=now,
                allowed=covers, query_endpoint=query_endpoint)
            fluent_values.extend(links.fluent_values)
            held = links.status == "held"
            configured_out = ConfiguredOut(
                device=body.scope.device, status=links.status,
                value=CONFIGURED_LINK_IN_FORCE if held else None,
                fluents=sorted({v.subject for v in links.fluent_values}),
                authority_cut=links.authority_cut)
            for row in recognised:
                result = reconcile(
                    EventFact(row.event_id, row.device, row.kind, row.event_time),
                    ConfiguredSide(body.scope.device, CONFIGURED_LINK_IN_FORCE if held else None),
                    now=now, max_age_s=times.limit_s)
                envelope_out.append(EventEnvelopeOut(event_id=row.event_id, node=row.device,
                                                     **result.envelope.to_dict()))
                conflicts_out.extend(
                    ConflictOut(event_id=row.event_id, device=row.device, event=row.kind,
                                configured=CONFIGURED_LINK_IN_FORCE, text=text)
                    for text in result.conflicts)
        events = EventsResult(
            window=EventWindow(start=_iso(start), end=_iso(times.valid_at), seconds=window_s),
            cap=EVENT_RESULT_CAP, truncated=truncated, events=returned, envelopes=envelope_out,
            configured=configured_out, conflicts=conflicts_out,
            promoted_conflicts=_promoted_conflicts(body.scope.device, times, now, caller.requester))
        eligibility["events"] = events_eligibility(
            count=len(returned), ambiguous=sum(e.mapping == "ambiguous" for e in returned),
            beyond_retention=start < now - timedelta(seconds=get_store().event_retention_s))

    knowledge = None
    if "knowledge" in body.classes:
        # Reached only after every refusal above. The files hold no history, so a question about the past
        # is answered as "not_current" without a retrieval and nothing is invented about it (D-37). For a
        # current question the service never raises: a fault is the outcome `unavailable`, answered with a
        # fixed gap while every other class answers as usual. The verdict was computed above and never
        # sees this branch.
        if times.historical:
            knowledge = KnowledgeResult(retrieval="not_current", cap=KNOWLEDGE_RESULT_CAP, truncated=False,
                                        references=[], withheld=0)
        else:
            found = get_service().retrieve(
                device=body.scope.device, interface=body.scope.interface, purpose=body.purpose, topic=body.topic,
                token_entities=authority.entities)
            knowledge = KnowledgeResult(
                retrieval=found.outcome, cap=KNOWLEDGE_RESULT_CAP, truncated=found.truncated,
                references=[_knowledge_ref(ref, now) for ref in found.references], withheld=found.withheld)
        eligibility["knowledge"] = knowledge_eligibility(
            outcome=knowledge.retrieval, applicable=len(knowledge.references))

    expires_at, basis = expiry(
        retrieved_at=now, observation_times=observation_times,
        configured_evidence=bool((graph is not None and graph.nodes) or fluent_values),
        limit_s=times.limit_s, historical=times.historical,
        event_evidence=events is not None and bool(events.events),
        knowledge_evidence=knowledge is not None and bool(knowledge.references))
    fluent_values.sort(key=lambda v: (v.context_class, v.kind, v.subject, v.valid_from or ""))
    response = ContextResponse(
        situation=Situation(
            id=identity, requester=caller.requester, purpose=body.purpose, scope=body.scope,
            intent_id=body.intent_id, classes=classes, max_depth=body.max_depth,
            relationship_types=relationship_types, valid_at=_iso(times.valid_at),
            known_at=_iso(times.known_at), retrieved_at=_iso(now), historical=times.historical,
            window_seconds=window_s, topic=body.topic),
        eligibility=overall({name: eligibility[name] for name in CONTEXT_CLASSES if name in eligibility}),
        expires_at=_iso(expires_at), expiry_basis=basis, fluent_values=fluent_values,
        envelopes=envelopes, graph=graph, events=events, knowledge=knowledge, verdict=verdict)
    # One row per answered request, written before the answer leaves (D-05). Every refusal and every
    # upstream failure happened above this line, so none of them records anything.
    get_store().record_situation(
        identity, stored_situation(response).model_dump_json(), expires_at=expires_at, recorded_at=now)
    return response


def fetch_situation(situation_id: str, caller: Caller, *, now: datetime | None = None
                    ) -> SituationRecord | None:
    """A recorded situation for its original requester, or None (the route answers 404).

    A malformed id, an unknown id, an id past its retention and an id someone else recorded are all
    None, so none of them can be told from another; so is a stored row that no longer validates (written
    by another image version), logged as one fixed line. The requester's own authority then has to cover the
    stored scope (the request's three fixed strings, in the request's order), and a situation that names
    an intent needs context:assess, as the request did. It reads only the state store and writes nothing."""
    if SITUATION_ID_PATTERN.match(situation_id) is None:
        return None
    now = (now if now is not None else _utcnow()).astimezone(timezone.utc)
    row = get_store().get_situation(situation_id, now=now)
    if row is None:
        return None
    try:
        stored = StoredSituation.model_validate_json(row.body)
    except ValidationError as exc:
        # A row an image of another version wrote (a field added or removed) or one damaged on disk. The
        # state policy is additive and rollback is an image rollback, so an older image meets rows it
        # cannot read. The answer is the 404 of an unknown id, which confirms nothing; the log line is
        # fixed text with the exception class, never the body or the id.
        log_fault(log, exc, "stored situation unreadable")
        return None
    if stored.situation.requester != caller.requester:
        return None  # before the authority checks, so a refusal never confirms that an id exists
    if caller.authority is None:
        raise Refused(AUTHORITY_REQUIRED)
    detail = authorize_scope(
        device=stored.situation.scope.device, purpose=stored.situation.purpose,
        classes=stored.situation.classes, authority=caller.authority)
    if detail is not None:
        raise Refused(detail)
    if stored.situation.intent_id is not None and SCOPE_ASSESS not in caller.scopes:
        raise Refused(INSUFFICIENT_SCOPE)
    return SituationRecord(
        **stored.model_dump(), expired=now >= row.expires_at, first_recorded_at=_iso(row.first_recorded_at),
        last_recorded_at=_iso(row.last_recorded_at), repeat_count=row.repeat_count)
