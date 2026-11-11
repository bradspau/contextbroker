"""Write a service binding for a TMF921 intent from the intent's current goals.

Reads the intent from the TMF921 server, takes its goals as the pinned values, checks each
placement against the network graph in Fuseki and stores the goal and the placement of each
metric as fluents (data_contracts/fluents.py): version nodes with a valid time and a
knowledge time. The whole change of one run is one SPARQL update, so Fuseki applies all of it
or none of it, and nothing is ever deleted or edited in place. The ABox file is written
only after the update is confirmed, so the file never gets ahead of the graph. The network
graph is not the TMF921 server's graph. The broker only reads the intent.

    python -m ingestion.bind_intent <intent_id> --place LatencyMetric=olt1/pon0
    python -m ingestion.bind_intent <intent_id> --place ... --replace [--valid-from TIME]
    python -m ingestion.bind_intent <intent_id> --place ... --correct

--replace: the goal (or the placement) really changed. The current fluent is closed at the
time of the run and a new one starts then; with --valid-from the new one starts at that
time and the old record is reissued so that what was known before the run is unchanged.
--correct: the earlier record was wrong. The old fluent is kept and marked superseded, and
the corrected one covers the same valid time, so a question about what was known before
the correction still gets the old value.
A run that changes nothing exits 0 and writes nothing. A binding is never reduced: if the
intent no longer has a metric the binding has, bind under a new intent id.

Without --valid-from, a question asked with a knowledge time before the run about a valid
time after the run is answered inconclusive (no goal in force), never with a wrong goal.
"""
import argparse
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from rdflib import RDF, Graph, URIRef

from broker_engine.binding_resolver import fetch_fluents
from broker_engine.sparql_resolver import FUSEKI_QUERY_ENDPOINT, fuseki_auth
from broker_engine.validation import REQUEST_TIMEOUT_S, ClientInputError
from data_contracts.fluents import (
    GUARD_BOTH,
    add_binding,
    add_goal,
    add_placement,
    close,
    guarded_insert,
    parse_dt,
    supersede,
)
from data_contracts.metric_map import check_token, check_uri, load_metric_map
from ingestion.load_ontologies import (
    ABOX_MARKER,
    DEFAULT_ABOX_DIR,
    DEFAULT_DATA_ENDPOINT,
    DEFAULT_SHACL_DIR,
    MissingCredential,
    fuseki_credentials,
    validate_shapes,
)
from intent_management.tmf921_client import (
    Tmf921Client,
    Tmf921Error,
    check_intent_id,
    intent_bounds,
)
from outcome_assurance.tmf921_report import _short_name

NS = "http://example.org/network-ontology#"

_VALID_FROM = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")


class BindError(ValueError):
    """The binding was refused; the message is safe to print."""


class BindFailed(BindError):
    """The run failed after the point of no return; the message is complete as printed."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ms(dt: datetime) -> datetime:
    """Whole milliseconds, the precision stored in the graph."""
    return dt.replace(microsecond=dt.microsecond // 1000 * 1000)


def _binding_name(intent_id: str, metric_uri: str) -> str:
    return f"binding-{intent_id}-{_short_name(metric_uri)}"


def _parse_valid_from(text: str) -> datetime:
    if not isinstance(text, str) or not _VALID_FROM.fullmatch(text):
        raise BindError("--valid-from must be a UTC time of the form YYYY-MM-DDTHH:MM:SSZ")
    try:
        return _ms(parse_dt(text))
    except ValueError:
        raise BindError("--valid-from must be a UTC time of the form YYYY-MM-DDTHH:MM:SSZ") from None


def _current(stored, uri, kind):
    """The one fluent of the metric and kind with no end and no supersession."""
    open_ = [x for x in stored.get((uri, kind), []) if x.end is None and x.superseded is None]
    if len(open_) != 1:
        raise BindError("the stored binding is not usable; nothing was changed")
    return open_[0]


def plan_changes(intent_id, bounds, placements, interface_iris, stored, now, *,
                 correct=False, valid_from=None):
    """The graph of one run: (graph, old fluent IRIs it closes or supersedes, guard predicates).

    bounds: metric URI -> number; placements: metric URI -> (device, interface);
    interface_iris: (device, interface) -> the interface node's IRI from the graph;
    stored: the fluents of the intent (fetch_fluents), empty for a first bind.
    A goal is compared as float and a placement by interface IRI. An empty graph means the
    binding already matches. Every recordedAt is `now`.
    """
    check_intent_id(intent_id)
    if not bounds:
        raise BindError("no readable goals")
    known = {m for m, _kind in stored}
    g = Graph()
    g.bind("", NS)
    olds: list[str] = []
    any_end = False
    names = set()
    for uri, bound in sorted(bounds.items()):
        try:
            check_uri(uri)
        except ValueError:
            raise BindError("a metric URI is not usable") from None
        if uri not in placements:
            raise BindError(f"no placement for {_short_name(uri)}")
        iri = interface_iris.get(placements[uri])
        if iri is None:
            raise BindError(f"no interface for {_short_name(uri)}")
        try:
            check_uri(iri, "interface iri")
        except ValueError:
            raise BindError("the interface IRI in the graph is not usable") from None
        name = _binding_name(intent_id, uri)
        if name in names:
            raise BindError("two metrics share a short name")
        names.add(name)
        base = NS + name
        try:
            goal = float(bound)
        except (TypeError, ValueError, OverflowError):
            raise BindError("a goal is not usable") from None

        def put(kind, value, start, end=None, *, reissue=False):
            if kind == "goal":
                add_goal(g, base, value, start, now, end,
                         kind="goal-reissue" if reissue else "goal")
            else:
                add_placement(g, base, value, start, now, end,
                              kind="place-reissue" if reissue else "place")

        if uri not in known:
            add_binding(g, base, intent_id, uri)
            start = valid_from or now
            put("goal", goal, start)
            put("placement", iri, start)
            continue
        for kind, wanted in (("goal", goal), ("placement", iri)):
            old = _current(stored, uri, kind)
            if old.value == wanted:
                continue
            if now <= old.start or now <= old.recorded:
                raise BindError("this run's time is not later than the stored binding; "
                                "nothing was changed")
            add_binding(g, base, intent_id, uri)
            olds.append(old.iri)
            if correct:
                supersede(g, old.iri, now)
                put(kind, wanted, old.start, old.end)
                any_end = any_end or old.end is not None
            elif valid_from is None:
                close(g, old.iri, now)
                put(kind, wanted, now)
            else:
                if valid_from <= old.start:
                    raise BindError("--valid-from is not later than when the current goal "
                                    "or placement started")
                supersede(g, old.iri, now)
                put(kind, old.value, old.start, valid_from, reissue=True)
                put(kind, wanted, valid_from)
    # A correction of a fluent that a replace already closed keeps its end, so only an
    # earlier correction may block it; otherwise any closing blocks (bead contextbroker-5xr).
    closed_by = ("supersededAt",) if correct and any_end else GUARD_BOTH
    return g, olds, closed_by


def parse_place(spec: str) -> tuple[str, str, str]:
    """'<ShortName>=<device>/<interface>' -> (short, device, interface)."""
    short, eq, rest = spec.partition("=")
    device, slash, iface = rest.partition("/")
    if not (eq and slash and short and device and iface):
        raise BindError(f"bad --place, expected Metric=device/interface: {spec!r}")
    try:
        check_token(short, "metric")
        check_token(device, "device")
        check_token(iface, "interface")
    except ValueError:
        raise BindError(f"unsafe value in --place: {spec!r}") from None
    return short, device, iface


def find_interface(device: str, iface: str, query_endpoint: str) -> str:
    """The interface node's IRI, or BindError if absent or ambiguous."""
    check_token(device, "device")
    check_token(iface, "interface")
    sparql = f"""
    PREFIX : <{NS}>
    SELECT ?i WHERE {{
      ?i a :Interface ; :onDevice "{device}" ; :interfaceName "{iface}" .
    }}
    """
    resp = requests.post(query_endpoint, data={"query": sparql},
                         headers={"Accept": "application/sparql-results+json"},
                         auth=fuseki_auth(), timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    try:
        rows = resp.json()["results"]["bindings"]
        iris = {r["i"]["value"] for r in rows if r["i"]["type"] == "uri"}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise BindError("the network graph returned an unusable answer") from None
    if not iris:
        raise BindError(f"interface {device}/{iface} is not in the topology")
    if len(iris) > 1:
        raise BindError(f"interface {device}/{iface} is ambiguous in the topology")
    return iris.pop()


def _post_sparql(query_endpoint, sparql):
    resp = requests.post(query_endpoint, data={"query": sparql},
                         headers={"Accept": "application/sparql-results+json"},
                         auth=fuseki_auth(), timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    try:
        return resp.json()["results"]["bindings"]
    except (KeyError, TypeError, ValueError, AttributeError):
        raise BindError("the network graph returned an unusable answer") from None


def derive_update_endpoint(data_endpoint: str) -> str:
    base = data_endpoint.rstrip("/")
    return (base[: -len("/data")] if base.endswith("/data") else base) + "/update"


def bind(intent_id, place_specs, *, abox_dir=DEFAULT_ABOX_DIR, write_only=False,
         query_endpoint=FUSEKI_QUERY_ENDPOINT, data_endpoint=DEFAULT_DATA_ENDPOINT,
         client=None, replace=False, correct=False, valid_from=None,
         update_endpoint=None, now=None) -> tuple[Path | None, bool]:
    """Returns (path written, applied). (None, False) means nothing changed.

    Raises BindError before anything changes, except BindFailed, which is raised after
    the update was sent. A stored binding is refused unless replace or correct is set.
    The whole change is one guarded update; the ABox file is written only after Fuseki
    confirmed it (or at once with write_only, which posts nothing). `now` is the one
    instant of the run: every recordedAt, and the end of a replaced fluent, is `now`.
    """
    check_intent_id(intent_id)
    if replace and correct:
        raise BindError("--replace and --correct cannot be used together")
    if valid_from is not None and (not replace or correct):
        raise BindError("--valid-from can only be used with --replace")
    run_at = _ms(now or _utcnow())
    start = None
    if valid_from is not None:
        start = _parse_valid_from(valid_from)
        if start > run_at:
            raise BindError("--valid-from is later than now")
    try:
        stored, _interfaces = fetch_fluents(intent_id, query_endpoint)
    except ValueError:
        raise BindError("the network graph returned an unusable answer") from None
    if stored and not (replace or correct):
        raise BindError("a binding for this intent already exists; use --replace if the goal "
                        "changed (the old goal is closed, nothing is deleted) or --correct if "
                        "the earlier record was wrong (the old record is kept, marked superseded)")
    if correct and not stored:
        raise BindError("there is no binding for this intent to correct")
    bounds = intent_bounds((client or Tmf921Client()).get_intent(intent_id))
    if not bounds:
        raise BindError("the intent's goals could not be read")
    try:
        mmap = load_metric_map()
    except ValueError:
        raise BindError("the metric map could not be read") from None
    for metric in sorted({m for m, _kind in stored}):
        if metric not in bounds:
            raise BindError(f"the intent no longer has {_short_name(metric)}; "
                            "bind it under a new intent id")
    by_short: dict[str, list[str]] = {}
    for uri in bounds:
        by_short.setdefault(_short_name(uri), []).append(uri)
    placements: dict[str, tuple[str, str]] = {}
    for spec in place_specs:
        short, device, iface = parse_place(spec)
        uris = by_short.get(short, [])
        if len(uris) != 1:
            raise BindError(f"metric {short!r} is unknown or ambiguous for this intent")
        if uris[0] in placements:
            raise BindError(f"metric {short!r} is placed twice")
        placements[uris[0]] = (device, iface)
    for uri in bounds:
        if uri not in placements:
            raise BindError(f"no placement for {_short_name(uri)}")
        if uri not in mmap:
            raise BindError(f"{_short_name(uri)} is not in the metric map")
    iris = {}
    for device, iface in set(placements.values()):
        iris[(device, iface)] = find_interface(device, iface, query_endpoint)
    graph, olds, closed_by = plan_changes(intent_id, bounds, placements, iris, stored, run_at,
                                          correct=correct, valid_from=start)
    if len(graph) == 0:
        return None, False

    shapes = sorted(Path(DEFAULT_SHACL_DIR).glob("*.ttl"))
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "binding.ttl"
        probe.write_text(graph.serialize(format="turtle"), encoding="utf-8")
        for s in shapes:
            try:
                ok, _report = validate_shapes(probe, s)
            except Exception:
                raise BindError("the binding could not be validated") from None
            if not ok:
                raise BindError("the binding failed SHACL validation")

    out_dir = Path(abox_dir)
    out = out_dir / f"{intent_id}.ttl"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise BindError("cannot write the binding file") from None
    if not write_only:
        _apply(graph, olds, closed_by, query_endpoint,
               update_endpoint or derive_update_endpoint(data_endpoint))
    try:
        out.write_text(ABOX_MARKER + "\n" + graph.serialize(format="turtle"), encoding="utf-8")
    except OSError:
        if write_only:
            raise BindError("cannot write the binding file") from None
        raise BindFailed("the change was applied to the network graph but the binding file "
                         "could not be written") from None
    return out, not write_only


def _apply(graph, olds, closed_by, query_endpoint, update_endpoint) -> None:
    """Send the one guarded update, then confirm that a new fluent is in the graph."""
    update = guarded_insert(graph, olds, closed_by=closed_by)
    fluent_types = (URIRef(NS + "GoalFluent"), URIRef(NS + "PlacementFluent"))
    probe = sorted(str(s) for t in fluent_types for s in graph.subjects(RDF.type, t))[0]
    try:
        resp = requests.post(update_endpoint, data=update.encode("utf-8"),
                             headers={"Content-Type": "application/sparql-update"},
                             auth=fuseki_credentials(), timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        rows = _post_sparql(query_endpoint, f"PREFIX : <{NS}>\n"
                            f"SELECT ?c WHERE {{ <{check_uri(probe, 'fluent iri')}> a ?c }}")
    except (requests.RequestException, BindError):
        raise BindFailed("the network graph did not confirm the change; running the same "
                         "command again reports an applied change as nothing changed") from None
    if not rows:
        raise BindFailed("the binding changed while it was being written; nothing was changed")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("intent_id")
    ap.add_argument("--place", action="append", default=[],
                    metavar="METRIC=DEVICE/INTERFACE")
    ap.add_argument("--write-only", action="store_true",
                    help="write the file only and send nothing to the network graph. The file "
                         "records one run; to apply a change run the same command without "
                         "--write-only (loading the file with load_ontologies does not close "
                         "the old goal)")
    ap.add_argument("--abox-dir", type=Path, default=DEFAULT_ABOX_DIR)
    ap.add_argument("--endpoint", default=DEFAULT_DATA_ENDPOINT,
                    help="Fuseki data endpoint (default FUSEKI_DATA_ENDPOINT; the update endpoint "
                         "is derived from it)")
    ap.add_argument("--query-endpoint", default=FUSEKI_QUERY_ENDPOINT,
                    help="Fuseki query endpoint (default FUSEKI_QUERY_ENDPOINT)")
    ap.add_argument("--update-endpoint", default=None,
                    help="Fuseki update endpoint (default: derived from --endpoint)")
    ap.add_argument("--replace", action="store_true",
                    help="the goal changed: the old goal is closed at the time of the run and "
                         "a new one starts; nothing is deleted")
    ap.add_argument("--correct", action="store_true",
                    help="the earlier record was wrong: the old record is kept and marked "
                         "superseded, and the corrected one covers the same time")
    ap.add_argument("--valid-from", default=None, metavar="TIME",
                    help="with --replace only: when the goal really changed, a UTC time such as "
                         "2026-11-05T10:00:00Z, not later than now")
    args = ap.parse_args(argv)
    try:
        fuseki_credentials()  # before any network call, including the TMF921 read
    except MissingCredential as exc:
        print(f"Refused: {exc}. Nothing was written or loaded.", file=sys.stderr)
        return 1
    try:
        out, applied = bind(args.intent_id, args.place, abox_dir=args.abox_dir,
                            write_only=args.write_only, data_endpoint=args.endpoint,
                            query_endpoint=args.query_endpoint, replace=args.replace,
                            correct=args.correct, valid_from=args.valid_from,
                            update_endpoint=args.update_endpoint)
    except BindFailed as exc:
        print(f"Failed: {exc}.", file=sys.stderr)
        return 1
    except BindError as exc:
        print(f"Refused: {exc}. Nothing was written or loaded.", file=sys.stderr)
        return 1
    except ClientInputError:
        print("Refused: invalid intent id. Nothing was written or loaded.", file=sys.stderr)
        return 1
    except Tmf921Error:
        print("Refused: the TMF921 server could not be read. Nothing was written or loaded.",
              file=sys.stderr)
        return 1
    except requests.RequestException:
        print("Refused: the network graph could not be reached. Nothing was written or loaded.",
              file=sys.stderr)
        return 1
    if out is None:
        print("Nothing changed: the binding already matches the intent.")
    elif applied:
        print(f"Wrote {out} and applied it to the network graph.")
    else:
        print(f"Wrote {out}. Nothing was applied (--write-only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
