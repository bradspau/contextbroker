"""The I/O shell of the context request (CTX-01 to CTX-08).

It composes the pure modules (the request contract, eligibility, the Context Graph, the binding
selection) over the sources: one Fuseki read for the intent's fluents, the Context Graph reads, and one
VictoriaMetrics read per placed metric, and for the events class one read of the state file (stored
syslog events, as structured fields and quality flags only, never the raw line or its message text).
It resolves, joins and delivers context and never decides or
acts: it pushes nothing to the TMF921 server, writes no high-water mark and records no outcome.

Every refusal (no authority claim, a request outside the caller's entities, purposes or classes, an
intent named without ``context:assess``, an offset beyond retention, a freshness above the default
limit) happens before the first Fuseki, VictoriaMetrics or TMF921 call (D-10). Nothing here logs the
request body, the requester or a claim value.
"""
import re
from datetime import datetime, timedelta, timezone

import requests

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
    observations_eligibility,
    overall,
    overlapping,
    topology_eligibility,
)
from broker_engine.sparql_resolver import FUSEKI_QUERY_ENDPOINT
from broker_engine.temporal import temporal_observation
from broker_engine.tsdb_joiner import join_observed_metric
from broker_engine.validation import ClientInputError
from data_contracts.context_contract import (
    CONTEXT_CLASSES,
    EVENT_RESULT_CAP,
    EVENT_SOURCE,
    ContextRequest,
    ContextResponse,
    EnvelopeOut,
    EventOut,
    EventsResult,
    EventWindow,
    FluentValue,
    Provenance,
    Situation,
    authorize,
    authorize_scope,
    expiry,
    resolve_times,
    situation_id,
)
from broker_state.process import event_window_limits, get_store
from data_contracts.metric_map import load_metric_map
from data_contracts.situation_record import (
    SITUATION_ID_PATTERN,
    SituationRecord,
    StoredSituation,
    stored_situation,
)
from intent_management.tmf921_source import assess_tmf921_as_of
from outcome_assurance.tmf921_report import _short_name

# The telemetry lookback is twice the default freshness limit, so a sample that is too old is read and
# reported stale (ineligible) instead of vanishing from the window and reading as missing (a gap).
OBSERVATION_LOOKBACK = "10m"

AUTHORITY_REQUIRED = "authority claims required"
INSUFFICIENT_SCOPE = "insufficient scope"
SITUATION_NOT_FOUND = "situation not found"


# A sending host is returned only when it is a plain token like a device name (research Pitfall 8).
_HOST_TOKEN = re.compile(r"[A-Za-z0-9_\-/]{1,128}")


class Refused(Exception):
    """A request the caller may not make. The message is always one of the fixed details."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def recording(join, seen):
    """Wrap a joiner so each distinct read happens once and its envelope is kept in `seen`.

    The key is (device, interface, metric, window, aggregate, as_of); a repeated call returns the
    envelope already read, so eligibility and a later verdict share one telemetry read."""
    def wrapped(device, interface, metric, window, **kw):
        key = (device, interface, metric, window, kw.get("aggregate"), kw.get("as_of"))
        if key not in seen:
            seen[key] = join(device, interface, metric, window, **kw)
        return seen[key]
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


def _fluent_value(metric, kind, fluent, device_interface, valid_at, retrieved_at) -> FluentValue:
    """One goal or placement fluent as a response value. A placement's value is its node id."""
    value = fluent.value if kind == "goal" else "/".join(device_interface)
    return FluentValue(
        context_class="goals", kind=kind, subject=_short_name(metric), value=value,
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

    classes = sorted(body.classes)
    relationship_types = None if body.relationship_types is None else sorted(body.relationship_types)
    identity = situation_id(
        requester=caller.requester, purpose=body.purpose, device=body.scope.device,
        interface=body.scope.interface, intent_id=body.intent_id, valid_at=times.valid_at,
        known_at=times.known_at, classes=classes, max_depth=body.max_depth,
        relationship_types=relationship_types)

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
                for uri in sorted(binding.bounds):
                    device, interface = binding.sources[uri]
                    mapped = mapping.get(uri)
                    if mapped is None or not covers(device):
                        continue
                    try:
                        observed[uri] = temporal_observation(
                            reader, device, interface, {"metric": mapped.broker_metric},
                            OBSERVATION_LOOKBACK, "at", body.as_of, now)
                    except ClientInputError:
                        continue  # left unobserved: a gap
                    nodes[uri] = f"{device}/{interface}"
                eligibility["observations"] = observations_eligibility(
                    sorted({metric for metric, _kind in fluents}), observed,
                    valid_at=times.valid_at, limit_s=times.limit_s)
                for uri, envelope in observed.items():
                    envelopes.append(EnvelopeOut(metric=_short_name(uri), node=nodes[uri],
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
        fluent_values.extend(read.fluent_values)
        eligibility["topology"] = topology_eligibility(
            read.scope_identity, conflicting_links=read.conflicting_links,
            colliding_ids=read.colliding_ids, malformed=read.malformed)

    events = None
    if "events" in body.classes:
        # The state file only: no Fuseki, VictoriaMetrics or TMF921 call. The window ends at the
        # resolved instant; one row past the cap tells whether the cap cut the result.
        window_s = event_window_limits()[0]
        start = times.valid_at - timedelta(seconds=window_s)
        rows = get_store().events_in_window(
            body.scope.device, start=start, end=times.valid_at, known_at=times.known_at, now=now,
            limit=EVENT_RESULT_CAP + 1)
        truncated = len(rows) > EVENT_RESULT_CAP
        returned = [_event_out(row, now, covers) for row in rows[:EVENT_RESULT_CAP]]
        events = EventsResult(
            window=EventWindow(start=_iso(start), end=_iso(times.valid_at), seconds=window_s),
            cap=EVENT_RESULT_CAP, truncated=truncated, events=returned)
        eligibility["events"] = events_eligibility(
            count=len(returned), ambiguous=sum(e.mapping == "ambiguous" for e in returned),
            beyond_retention=False)

    expires_at, basis = expiry(
        retrieved_at=now, observation_times=observation_times,
        configured_evidence=bool((graph is not None and graph.nodes) or fluent_values),
        limit_s=times.limit_s, historical=times.historical)
    fluent_values.sort(key=lambda v: (v.context_class, v.kind, v.subject, v.valid_from or ""))
    response = ContextResponse(
        situation=Situation(
            id=identity, requester=caller.requester, purpose=body.purpose, scope=body.scope,
            intent_id=body.intent_id, classes=classes, max_depth=body.max_depth,
            relationship_types=relationship_types, valid_at=_iso(times.valid_at),
            known_at=_iso(times.known_at), retrieved_at=_iso(now), historical=times.historical),
        eligibility=overall({name: eligibility[name] for name in CONTEXT_CLASSES if name in eligibility}),
        expires_at=_iso(expires_at), expiry_basis=basis, fluent_values=fluent_values,
        envelopes=envelopes, graph=graph, events=events, verdict=verdict)
    # One row per answered request, written before the answer leaves (D-05). Every refusal and every
    # upstream failure happened above this line, so none of them records anything.
    get_store().record_situation(
        identity, stored_situation(response).model_dump_json(), expires_at=expires_at, recorded_at=now)
    return response


def fetch_situation(situation_id: str, caller: Caller, *, now: datetime | None = None
                    ) -> SituationRecord | None:
    """A recorded situation for its original requester, or None (the route answers 404).

    A malformed id, an unknown id, an id past its retention and an id someone else recorded are all
    None, so none of them can be told from another. The requester's own authority then has to cover the
    stored scope (the request's three fixed strings, in the request's order), and a situation that names
    an intent needs context:assess, as the request did. It reads only the state store and writes nothing."""
    if SITUATION_ID_PATTERN.match(situation_id) is None:
        return None
    now = (now if now is not None else _utcnow()).astimezone(timezone.utc)
    row = get_store().get_situation(situation_id, now=now)
    if row is None:
        return None
    stored = StoredSituation.model_validate_json(row.body)
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
