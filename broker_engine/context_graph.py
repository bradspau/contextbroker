"""The Context Graph: the declared topology as nodes and edges at a valid and knowledge time.

A purpose-built, wire-neutral view of the topology in Fuseki (CTX-07, CTX-08). The graph has no
device node, only the ``:onDevice`` literal on an ``:Interface``, so devices are synthesised from
that literal. Node ids are derived (D-25): a device id is the ``:onDevice`` name, an interface id is
``device/interfaceName`` with the interface IRI local name as ``graph_ref``; a link stays an edge
attribute (``link`` and ``fluent`` are IRI local names).

SPARQL answers one level at a time (one device query and one link query at most) and the limits are
policy applied here in Python: depth, relationship types, the node cap and authority over what the
traversal discovers (D-23, D-26). Link fluents are selected in Python at the valid and knowledge
time (``Fluent.covers`` and ``Fluent.known``) because engines disagree on zoned ``xsd:dateTime``
comparison. After the last allowed level one look-ahead round expands once more without adding a
node, only to learn whether depth cut something allowed.

This module returns findings as flags (``scope_identity``, ``conflicting_links``,
``colliding_ids``, ``malformed``) and never reason text; plan 03-04's ``topology_eligibility`` turns
them into fixed reasons. It reads and never writes. A device string or IRI reaches a query only
after ``check_token`` or ``check_uri``, including values read back from Fuseki before they are
reused in the next level's query; a value that fails is reported as a fixed
``malformed topology result`` and is never echoed.
"""
from dataclasses import dataclass
from datetime import datetime

import requests  # noqa: F401 -- tests patch broker_engine.context_graph.requests.post

from broker_engine import upstream_http
from broker_engine.sparql_resolver import FUSEKI_QUERY_ENDPOINT, fuseki_auth
from broker_engine.validation import REQUEST_TIMEOUT_S
from data_contracts.context_contract import (
    MAX_DEPTH,
    NODE_CAP,
    RELATIONSHIP_TYPES,
    ContextGraph,
    FluentValue,
    GraphEdge,
    GraphLimits,
    GraphNode,
    Provenance,
)
from data_contracts.fluents import NS, Fluent, parse_dt
from data_contracts.metric_map import check_token, check_uri

MALFORMED = "malformed topology result"
_ACCEPT = {"Accept": "application/sparql-results+json"}


class MalformedTopology(ValueError):
    """A value read back is unusable. The message is fixed and never carries the value."""

    def __init__(self):
        super().__init__(MALFORMED)


def local_name(iri: str) -> str:
    """The part of an IRI after ``#``, else after the last ``/``."""
    if "#" in iri:
        return iri.rsplit("#", 1)[1]
    return iri.rsplit("/", 1)[-1]


# ---- the readers -------------------------------------------------------------------------


def _select(query_endpoint, sparql) -> list[dict]:
    response = upstream_http.post("fuseki", query_endpoint, data={"query": sparql},
                                  headers=_ACCEPT, auth=fuseki_auth(),
                                  timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    try:
        bindings = response.json()["results"]["bindings"]
    except (KeyError, TypeError, ValueError):
        raise MalformedTopology() from None
    if not isinstance(bindings, list):
        raise MalformedTopology()
    return bindings


def _value(binding, name, *, required=True):
    """The string value of a bound variable, None if optional and unbound; Malformed otherwise."""
    term = binding.get(name) if isinstance(binding, dict) else None
    if term is None:
        if required:
            raise MalformedTopology()
        return None
    value = term.get("value") if isinstance(term, dict) else None
    if not isinstance(value, str):
        raise MalformedTopology()
    return value


def _checked(check, value, label):
    """``check(value, label)`` or a fixed Malformed; None passes through."""
    if value is None:
        return None
    try:
        return check(value, label)
    except ValueError:
        raise MalformedTopology() from None


def _time(value):
    if value is None:
        return None
    try:
        return parse_dt(value)
    except ValueError:
        raise MalformedTopology() from None


def read_device_interfaces(devices, query_endpoint=FUSEKI_QUERY_ENDPOINT) -> list[tuple[str, str, str]]:
    """One SELECT for every interface of the given devices: ``(iri, device, interfaceName)`` rows
    sorted by IRI. An empty list of devices sends no query."""
    devices = sorted({check_token(d, "device") for d in devices})
    if not devices:
        return []
    values = " ".join(f'"{d}"' for d in devices)
    sparql = f"""
    PREFIX : <{NS}>
    SELECT ?i ?d ?n WHERE {{
      VALUES ?d {{ {values} }}
      ?i a :Interface ; :onDevice ?d ; :interfaceName ?n .
    }}"""
    out = []
    for binding in _select(query_endpoint, sparql):
        iri = _checked(check_uri, _value(binding, "i"), "interface iri")
        device = _checked(check_token, _value(binding, "d"), "device")
        name = _checked(check_token, _value(binding, "n"), "interface")
        if device not in devices:
            raise MalformedTopology()
        out.append((iri, device, name))
    return sorted(out)


def read_device_names(query_endpoint=FUSEKI_QUERY_ENDPOINT) -> list[str]:
    """One SELECT for the distinct ``:onDevice`` values of every ``:Interface``: the device table
    (D-07). Each value must pass the token check, else the fixed Malformed; the list is sorted
    and distinct, and an empty graph gives ``[]``. It reads and never writes."""
    sparql = f"""
    PREFIX : <{NS}>
    SELECT DISTINCT ?d WHERE {{
      ?i a :Interface ; :onDevice ?d .
    }}"""
    names = set()
    for binding in _select(query_endpoint, sparql):
        names.add(_checked(check_token, _value(binding, "d"), "device"))
    return sorted(names)


def read_links(interface_iris, query_endpoint=FUSEKI_QUERY_ENDPOINT) -> list[dict]:
    """One SELECT for every link fluent that names one of the given interfaces, with no time filter.

    One dict per ``(fluent, self, other)``: ``fluent``, ``link``, ``self``, ``self_device``,
    ``self_name``, ``other`` (None when the fluent names no second interface), ``other_device`` and
    ``other_name`` (None when the other interface is not described), and the four times as aware
    datetimes (``start``, ``end``, ``recorded``, ``superseded``; the two ends may be None). Time
    selection is ``select_links``. An empty list of IRIs sends no query."""
    wanted = sorted({check_uri(i, "interface iri") for i in interface_iris})
    if not wanted:
        return []
    values = " ".join(f"<{i}>" for i in wanted)
    sparql = f"""
    PREFIX : <{NS}>
    SELECT ?lf ?link ?self ?sd ?sn ?other ?od ?on ?vs ?ve ?ra ?sa WHERE {{
      VALUES ?self {{ {values} }}
      ?lf a :LinkFluent ; :ofLink ?link ; :hasInterface ?self ; :validStart ?vs ; :recordedAt ?ra .
      ?self :onDevice ?sd ; :interfaceName ?sn .
      OPTIONAL {{ ?lf :validEnd ?ve }}
      OPTIONAL {{ ?lf :supersededAt ?sa }}
      OPTIONAL {{
        ?lf :hasInterface ?other .
        FILTER(?other != ?self)
        OPTIONAL {{ ?other :onDevice ?od ; :interfaceName ?on }}
      }}
    }}"""
    rows = []
    for binding in _select(query_endpoint, sparql):
        own = _checked(check_uri, _value(binding, "self"), "interface iri")
        if own not in wanted:
            raise MalformedTopology()
        rows.append({
            "fluent": _checked(check_uri, _value(binding, "lf"), "fluent iri"),
            "link": _checked(check_uri, _value(binding, "link"), "link iri"),
            "self": own,
            "self_device": _checked(check_token, _value(binding, "sd"), "device"),
            "self_name": _checked(check_token, _value(binding, "sn"), "interface"),
            "other": _checked(check_uri, _value(binding, "other", required=False), "interface iri"),
            "other_device": _checked(check_token, _value(binding, "od", required=False), "device"),
            "other_name": _checked(check_token, _value(binding, "on", required=False), "interface"),
            "start": _time(_value(binding, "vs")),
            "end": _time(_value(binding, "ve", required=False)),
            "recorded": _time(_value(binding, "ra")),
            "superseded": _time(_value(binding, "sa", required=False)),
        })
    # A fluent that reads back with two different links or times is not one record.
    seen = {}
    for r in rows:
        mark = (r["link"], r["start"], r["end"], r["recorded"], r["superseded"])
        if seen.setdefault(r["fluent"], mark) != mark:
            raise MalformedTopology()
    rows.sort(key=lambda r: (r["fluent"], r["self"], r["other"] or ""))
    return rows


# ---- selecting link fluents in Python ----------------------------------------------------


@dataclass(frozen=True)
class LinkEnd:
    iri: str
    device: str
    name: str

    @property
    def node_id(self) -> str:
        return f"{self.device}/{self.name}"


@dataclass(frozen=True)
class SelectedLink:
    """A link fluent in force with its two interface ends in sorted node id order."""

    fluent: Fluent
    link: str
    ends: tuple[LinkEnd, LinkEnd]


@dataclass(frozen=True)
class LinkSelection:
    links: tuple[SelectedLink, ...]
    conflicting: bool


def select_links(rows, valid_at: datetime, known_at: datetime | None) -> LinkSelection:
    """The link fluents in force at ``valid_at`` as known at ``known_at`` (None is the latest).

    Pure. ``conflicting`` is set when one link has more than one distinct fluent in force, or an
    in-force fluent does not name exactly two described interfaces; such a fluent gives no link."""
    by_fluent: dict[str, list[dict]] = {}
    for r in rows:
        by_fluent.setdefault(r["fluent"], []).append(r)
    conflicting = False
    in_force: list[tuple[Fluent, list[dict]]] = []
    for iri in sorted(by_fluent):
        group = by_fluent[iri]
        first = group[0]
        fluent = Fluent(iri, first["link"], first["start"], first["end"], first["recorded"],
                        first["superseded"])
        if fluent.known(known_at) and fluent.covers(valid_at):
            in_force.append((fluent, group))
    per_link: dict[str, int] = {}
    for fluent, _ in in_force:
        per_link[fluent.value] = per_link.get(fluent.value, 0) + 1
    if any(count > 1 for count in per_link.values()):
        conflicting = True
    links = []
    for fluent, group in in_force:
        ends: dict[str, LinkEnd] = {}
        complete = True
        for r in group:
            ends[r["self"]] = LinkEnd(r["self"], r["self_device"], r["self_name"])
            if r["other"] is None:
                continue
            if r["other_device"] is None or r["other_name"] is None:
                complete = False
                ends.setdefault(r["other"], LinkEnd(r["other"], "", ""))
            else:
                ends[r["other"]] = LinkEnd(r["other"], r["other_device"], r["other_name"])
        if len(ends) != 2 or not complete:
            conflicting = True
            continue
        pair = tuple(sorted(ends.values(), key=lambda e: e.node_id))
        links.append(SelectedLink(fluent, fluent.value, pair))
    return LinkSelection(tuple(links), conflicting)


def _link_value(link: SelectedLink, first, second, retrieved_at, context_class) -> FluentValue:
    """A link fluent as a response value: the link local name, the two node ids, the fluent's valid
    and recorded times and a Fuseki provenance record naming the fluent IRI."""
    fluent = link.fluent

    def stamp(moment):
        return moment.isoformat() if moment else None

    return FluentValue(
        context_class=context_class, kind="link", subject=local_name(link.link),
        value=[first, second], in_force=True, valid_from=stamp(fluent.start),
        valid_to=stamp(fluent.end), recorded_at=stamp(fluent.recorded),
        superseded_at=stamp(fluent.superseded),
        provenance=Provenance(source_system="fuseki", reference=fluent.iri,
                              observed_at=stamp(fluent.recorded), retrieved_at=stamp(retrieved_at)))


# ---- the traversal -----------------------------------------------------------------------


@dataclass(frozen=True)
class GraphResult:
    graph: ContextGraph
    fluent_values: list[FluentValue]
    scope_identity: str
    conflicting_links: bool
    colliding_ids: bool
    malformed: bool


@dataclass(frozen=True)
class _Target:
    id: str
    type: str
    device: str
    interface: str | None
    iri: str | None


@dataclass(frozen=True)
class _Candidate:
    type: str
    ends: tuple[_Target, _Target]
    link: SelectedLink | None = None


@dataclass
class _Node:
    id: str
    type: str
    device: str
    interface: str | None
    iri: str | None
    depth: int


def _device_target(device):
    return _Target(device, "device", device, None, None)


class _Traversal:
    def __init__(self, device, interface, *, max_depth, types, valid_at, known_at, retrieved_at,
                 allowed, priority, node_cap, query_endpoint):
        self.device, self.interface = device, interface
        self.max_depth, self.types = max_depth, frozenset(types)
        self.valid_at, self.known_at, self.retrieved_at = valid_at, known_at, retrieved_at
        self.allowed, self.priority, self.node_cap = allowed, frozenset(priority), node_cap
        self.endpoint = query_endpoint
        self.nodes: dict[str, _Node] = {}
        self.edges: dict[tuple, GraphEdge] = {}
        self.values: dict[tuple, FluentValue] = {}
        self.cut: set[str] = set()
        self.scope = "missing"
        self.conflicting = False
        self.collisions: set[str] = set()
        self.ambiguous_id: str | None = None
        self._device_rows: dict[str, dict[str, list[str]]] = {}
        self._iri_of: dict[str, str] = {}

    # identity ---------------------------------------------------------------------------

    def _note(self, node_id, iri):
        if self._iri_of.setdefault(node_id, iri) != iri:
            self.collisions.add(node_id)

    def _interfaces_of(self, devices) -> None:
        """Make the interface rows of every named device available; one query for the new ones."""
        fresh = sorted({d for d in devices if d not in self._device_rows})
        if not fresh:
            return
        found = {d: {} for d in fresh}
        for iri, device, name in read_device_interfaces(fresh, self.endpoint):
            found[device].setdefault(name, []).append(iri)
            self._note(f"{device}/{name}", iri)
        self._device_rows.update(found)

    # expansion --------------------------------------------------------------------------

    def _links_of(self, interface_nodes) -> LinkSelection:
        rows = read_links([n.iri for n in interface_nodes], self.endpoint)
        selection = select_links(rows, self.valid_at, self.known_at)
        self.conflicting = self.conflicting or selection.conflicting
        return selection

    def _candidates(self, frontier, selection) -> list[_Candidate]:
        out = []
        for node in frontier:
            if node.type == "device":
                for name in sorted(self._device_rows[node.device]):
                    iri = self._device_rows[node.device][name][0]
                    target = _Target(f"{node.device}/{name}", "interface", node.device, name, iri)
                    out.append(_Candidate("has_interface", (_device_target(node.device), target)))
            else:
                target = _Target(node.id, "interface", node.device, node.interface, node.iri)
                out.append(_Candidate("has_interface", (_device_target(node.device), target)))
        for link in selection.links:
            ends = tuple(_Target(e.node_id, "interface", e.device, e.name, e.iri) for e in link.ends)
            for end in link.ends:
                self._note(end.node_id, end.iri)
            out.append(_Candidate("connected_to", ends, link))
        return out

    def _expand(self, frontier, depth, *, commit) -> list[_Node]:
        """One level. With ``commit`` new nodes join at ``depth + 1``; without it (the look-ahead)
        nothing is added and ``depth`` is named in ``cut`` if an allowed node was beyond reach."""
        self._interfaces_of([n.device for n in frontier if n.type == "device"])
        interface_nodes = [n for n in frontier if n.type == "interface"]
        selection = self._links_of(interface_nodes) if interface_nodes else LinkSelection((), False)
        new: dict[str, _Target] = {}
        wanted: list[_Candidate] = []
        for cand in self._candidates(frontier, selection):
            fresh = [t for t in cand.ends if t.id not in self.nodes]
            if any(not self.allowed(t.device) for t in fresh):
                if commit:
                    self.cut.add("authority")
                continue
            if cand.type not in self.types:
                if commit:
                    self.cut.add("relationship_types")
                continue
            wanted.append(cand)
            for target in fresh:
                new[target.id] = target
        added: list[_Node] = []
        if commit:
            ordered = sorted(new.values(), key=lambda t: (t.id not in self.priority, t.id))
            room = max(self.node_cap - len(self.nodes), 0)
            if len(ordered) > room:
                self.cut.add("node_cap")
            for t in ordered[:room]:
                node = _Node(t.id, t.type, t.device, t.interface, t.iri, depth + 1)
                self.nodes[t.id] = node
                added.append(node)
        elif new:
            self.cut.add("depth")
        for cand in wanted:
            self._add_edge(cand)
        return added

    def _add_edge(self, cand: _Candidate) -> None:
        first, second = cand.ends
        if first.id not in self.nodes or second.id not in self.nodes:
            return
        link = cand.link
        edge = GraphEdge(from_=first.id, to=second.id, type=cand.type,
                         link=local_name(link.link) if link else None,
                         fluent=local_name(link.fluent.iri) if link else None)
        key = (edge.type, edge.from_, edge.to, edge.link or "", edge.fluent or "")
        if key in self.edges:
            return
        self.edges[key] = edge
        if link is not None:
            self.values[key] = self._fluent_value(link, first.id, second.id)

    def _fluent_value(self, link: SelectedLink, first, second) -> FluentValue:
        return _link_value(link, first, second, self.retrieved_at, "topology")

    # the walk ---------------------------------------------------------------------------

    def run(self) -> None:
        self._interfaces_of([self.device])
        rows = self._device_rows[self.device]
        if self.interface is None:
            if not rows:
                return
            root = _Node(self.device, "device", self.device, None, None, 0)
        else:
            matches = rows.get(self.interface, [])
            scope_id = f"{self.device}/{self.interface}"
            if not matches:
                return
            if len(set(matches)) > 1:
                self.scope, self.ambiguous_id = "ambiguous", scope_id
                return
            root = _Node(scope_id, "interface", self.device, self.interface, matches[0], 0)
        self.scope = "resolved"
        self.nodes[root.id] = root
        frontier = [root]
        for depth in range(self.max_depth):
            if not frontier:
                break
            frontier = self._expand(frontier, depth, commit=True)
        if frontier:
            self._expand(frontier, self.max_depth, commit=False)

    def result(self, *, malformed=False) -> GraphResult:
        if malformed:
            self.nodes, self.edges, self.values, self.cut = {}, {}, {}, set()
        limits = GraphLimits(requested_depth=self.max_depth, max_depth=MAX_DEPTH,
                             node_cap=self.node_cap, relationship_types=sorted(self.types),
                             cut_by=sorted(self.cut))
        nodes = [GraphNode(id=n.id, type=n.type, device=n.device, interface=n.interface,
                           graph_ref=local_name(n.iri) if n.type == "interface" else None,
                           depth=n.depth)
                 for n in sorted(self.nodes.values(), key=lambda n: (n.depth, n.id))]
        order = sorted(self.edges, key=lambda k: k)
        edges = [self.edges[k] for k in order]
        values = [self.values[k] for k in order if k in self.values]
        colliding = bool(self.collisions - {self.ambiguous_id})
        return GraphResult(ContextGraph(nodes=nodes, edges=edges, limits=limits), values,
                           self.scope, self.conflicting, colliding, malformed)


def build_graph(device, interface, *, max_depth, relationship_types, valid_at, known_at,
                retrieved_at, allowed, priority=frozenset(), node_cap=NODE_CAP,
                query_endpoint=FUSEKI_QUERY_ENDPOINT) -> GraphResult:
    """The Context Graph around a device (``interface`` None) or one of its interfaces.

    Breadth-first from the scope, at most ``max_depth`` levels plus one look-ahead round, and at
    most one device query and one link query per level. ``allowed(device)`` says whether the
    caller's authority covers a device the traversal discovers; the scope device itself was
    authorised before any upstream call. ``priority`` holds the node ids that carry envelopes: when
    the node cap bites they are kept before others at the same level. A request for more than
    MAX_DEPTH never reaches here (the request model refuses it)."""
    check_token(device, "device")
    if interface is not None:
        check_token(interface, "interface")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 0 <= max_depth <= MAX_DEPTH:
        raise ValueError("max_depth out of range")
    if isinstance(node_cap, bool) or not isinstance(node_cap, int) or node_cap < 1:
        raise ValueError("node_cap out of range")
    types = list(relationship_types)
    if len(set(types)) != len(types) or any(t not in RELATIONSHIP_TYPES for t in types):
        raise ValueError("unknown relationship type")
    for moment in (valid_at, known_at):
        if moment is not None and (not isinstance(moment, datetime) or moment.utcoffset() is None):
            raise ValueError("timestamp needs a zone")
    walk = _Traversal(device, interface, max_depth=max_depth, types=types, valid_at=valid_at,
                      known_at=known_at, retrieved_at=retrieved_at, allowed=allowed,
                      priority=priority, node_cap=node_cap, query_endpoint=query_endpoint)
    try:
        walk.run()
    except MalformedTopology:
        return walk.result(malformed=True)
    return walk.result()


# ---- the configured side of an event reconciliation --------------------------------------


@dataclass(frozen=True)
class DeviceLinks:
    """The link fluents in force that have an end on one device.

    ``status`` is ``held`` (at least one link kept), ``not_held`` (none in force, or every one cut by
    authority) or ``invalid`` (conflicting or malformed link records; no values). ``authority_cut``
    says a link was dropped because its other end lies outside the caller's entities."""

    status: str
    fluent_values: list[FluentValue]
    authority_cut: bool


def device_links_in_force(device, *, valid_at: datetime, known_at: datetime | None,
                          retrieved_at: datetime, allowed,
                          query_endpoint=FUSEKI_QUERY_ENDPOINT) -> DeviceLinks:
    """The link fluents in force at ``valid_at`` as known at ``known_at`` with an end on ``device``.

    One interface query and, when the device has interfaces, one link query. A link whose other end's
    device is not covered by ``allowed`` is dropped and only sets ``authority_cut``: it is neither
    shown nor counted (D-26). An end on the device itself is always covered. A malformed read back
    or a conflicting selection is ``invalid``; a Fuseki failure propagates as ``requests``'s error.
    It reads and never writes."""
    check_token(device, "device")
    for moment in (valid_at, known_at):
        if moment is not None and (not isinstance(moment, datetime) or moment.utcoffset() is None):
            raise ValueError("timestamp needs a zone")
    try:
        interfaces = read_device_interfaces([device], query_endpoint)
        selection = select_links(read_links([iri for iri, _d, _n in interfaces], query_endpoint),
                                 valid_at, known_at)
    except MalformedTopology:
        return DeviceLinks("invalid", [], False)
    if selection.conflicting:
        return DeviceLinks("invalid", [], False)
    values, cut = [], False
    for link in selection.links:
        if all(end.device == device or allowed(end.device) for end in link.ends):
            first, second = link.ends
            values.append(_link_value(link, first.node_id, second.node_id, retrieved_at, "events"))
        else:
            cut = True
    values.sort(key=lambda v: (v.subject, v.provenance.reference))
    return DeviceLinks("held" if values else "not_held", values, cut)
