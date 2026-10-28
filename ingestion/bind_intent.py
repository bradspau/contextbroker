"""Write a service binding for a TMF921 intent from the intent's current bounds.

Reads the intent from the TMF921 server, takes its bounds as the pinned values,
checks each placement against the network graph in Fuseki, validates the
Turtle against the SHACL shapes, writes it to the ABox directory and loads it.
On any refusal nothing is written and nothing is loaded. The network graph is
not the TMF921 server's graph.

    python -m ingestion.bind_intent <intent_id> --place LatencyMetric=olt1/pon0 [--write-only]
"""
import argparse
import sys
import tempfile
from pathlib import Path

import requests

from broker_engine.sparql_resolver import FUSEKI_QUERY_ENDPOINT
from broker_engine.validation import REQUEST_TIMEOUT_S, ClientInputError
from data_contracts.metric_map import _check_token, check_uri, load_metric_map
from ingestion.load_ontologies import (
    DEFAULT_ABOX_DIR,
    DEFAULT_DATA_ENDPOINT,
    DEFAULT_SHACL_DIR,
    load_ttl_to_fuseki,
    validate_shapes,
)
from intent_management.tmf921_client import (
    Tmf921Client,
    Tmf921Error,
    _check_intent_id,
    intent_bounds,
)
from outcome_assurance.tmf921_report import _short_name

NS = "http://example.org/network-ontology#"


class LoadError(Exception):
    """The file was written but the network graph did not accept it."""

    def __init__(self, path, removed=False, delete_failed=False):
        super().__init__("load failed")
        self.path = path
        self.removed = removed
        self.delete_failed = delete_failed


class BindError(ValueError):
    """The binding was refused; the message is safe to print."""


def _binding_name(intent_id: str, metric_uri: str) -> str:
    return f"binding-{intent_id}-{_short_name(metric_uri)}"


def render_binding(intent_id, bounds, placements, interface_iris) -> str:
    """Turtle for one :ServiceBinding per metric.

    bounds: metric URI -> number; placements: metric URI -> (device, interface);
    interface_iris: (device, interface) -> the interface node's IRI from the graph.
    """
    _check_intent_id(intent_id)
    if not bounds:
        raise BindError("no readable bounds")
    lines = [f"@prefix : <{NS}> .",
             "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .", ""]
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
        lines += [f":{name} a :ServiceBinding ;",
                  f'    :forIntent "{intent_id}" ;',
                  f'    :forMetric "{uri}" ;',
                  f"    :onInterface <{iri}> ;",
                  f'    :pinnedBound "{float(bound)!r}"^^xsd:double .', ""]
    return "\n".join(lines)


def parse_place(spec: str) -> tuple[str, str, str]:
    """'<ShortName>=<device>/<interface>' -> (short, device, interface)."""
    short, eq, rest = spec.partition("=")
    device, slash, iface = rest.partition("/")
    if not (eq and slash and short and device and iface):
        raise BindError(f"bad --place, expected Metric=device/interface: {spec!r}")
    try:
        _check_token(short, "metric")
        _check_token(device, "device")
        _check_token(iface, "interface")
    except ValueError:
        raise BindError(f"unsafe value in --place: {spec!r}") from None
    return short, device, iface


def find_interface(device: str, iface: str, query_endpoint: str) -> str:
    """The interface node's IRI, or BindError if absent or ambiguous."""
    _check_token(device, "device")
    _check_token(iface, "interface")
    sparql = f"""
    PREFIX : <{NS}>
    SELECT ?i WHERE {{
      ?i a :Interface ; :onDevice "{device}" ; :interfaceName "{iface}" .
    }}
    """
    resp = requests.post(query_endpoint, data={"query": sparql},
                         headers={"Accept": "application/sparql-results+json"},
                         timeout=REQUEST_TIMEOUT_S)
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
                         timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    try:
        return resp.json()["results"]["bindings"]
    except (KeyError, TypeError, ValueError, AttributeError):
        raise BindError("the network graph returned an unusable answer") from None


def binding_exists(intent_id: str, query_endpoint: str) -> bool:
    _check_intent_id(intent_id)  # only the validated id enters the query
    rows = _post_sparql(query_endpoint, f"""
    PREFIX : <{NS}>
    SELECT ?b WHERE {{ ?b a :ServiceBinding ; :forIntent "{intent_id}" . }}
    """)
    return bool(rows)


def delete_bindings(intent_id: str, update_endpoint: str, auth=("admin", "admin")) -> None:
    """Delete every :ServiceBinding node for the intent, with all its triples."""
    _check_intent_id(intent_id)
    update = f"""
    PREFIX : <{NS}>
    DELETE {{ ?b ?p ?o }} WHERE {{
      ?b a :ServiceBinding ; :forIntent "{intent_id}" ; ?p ?o .
    }}
    """
    try:
        resp = requests.post(update_endpoint, data=update.encode("utf-8"),
                             headers={"Content-Type": "application/sparql-update"},
                             auth=auth, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
    except requests.RequestException:
        raise BindError("the existing binding could not be deleted") from None


def derive_update_endpoint(data_endpoint: str) -> str:
    base = data_endpoint.rstrip("/")
    return (base[: -len("/data")] if base.endswith("/data") else base) + "/update"


def bind(intent_id, place_specs, *, abox_dir=DEFAULT_ABOX_DIR, write_only=False,
         query_endpoint=FUSEKI_QUERY_ENDPOINT, data_endpoint=DEFAULT_DATA_ENDPOINT,
         client=None, replace=False, update_endpoint=None) -> tuple[Path, bool]:
    """Returns (path written, loaded). Raises BindError before writing anything.

    An existing binding for the intent is refused unless replace is set. With
    replace (and not write_only) the old binding nodes are deleted from the
    network graph after the new one validates and before anything is written.
    write_only only reads Fuseki, so it never deletes.
    """
    _check_intent_id(intent_id)
    exists = binding_exists(intent_id, query_endpoint)
    if exists and not replace:
        raise BindError("a binding for this intent already exists; use --replace to replace it")
    bounds = intent_bounds((client or Tmf921Client()).get_intent(intent_id))
    if not bounds:
        raise BindError("the intent's bounds could not be read")
    try:
        mmap = load_metric_map()
    except ValueError:
        raise BindError("the metric map could not be read") from None
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
    ttl = render_binding(intent_id, bounds, placements, iris)

    shapes = sorted(Path(DEFAULT_SHACL_DIR).glob("*.ttl"))
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "binding.ttl"
        probe.write_text(ttl, encoding="utf-8")
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
        out.write_text(ttl, encoding="utf-8")
    except OSError:
        raise BindError("cannot write the binding file") from None
    if write_only:
        return out, False
    removed = False
    if exists and replace:
        try:
            delete_bindings(intent_id, update_endpoint or derive_update_endpoint(data_endpoint))
        except BindError:
            raise LoadError(out, removed=False, delete_failed=True) from None
        removed = True
    try:
        load_ttl_to_fuseki(out, data_endpoint)
    except requests.RequestException:
        raise LoadError(out, removed=removed) from None
    return out, True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("intent_id")
    ap.add_argument("--place", action="append", default=[],
                    metavar="METRIC=DEVICE/INTERFACE")
    ap.add_argument("--write-only", action="store_true")
    ap.add_argument("--abox-dir", type=Path, default=DEFAULT_ABOX_DIR)
    ap.add_argument("--endpoint", default=DEFAULT_DATA_ENDPOINT, help="Fuseki data endpoint")
    ap.add_argument("--query-endpoint", default=FUSEKI_QUERY_ENDPOINT,
                    help="Fuseki query endpoint (default FUSEKI_QUERY_ENDPOINT)")
    ap.add_argument("--update-endpoint", default=None,
                    help="Fuseki update endpoint (default: derived from --endpoint)")
    ap.add_argument("--replace", action="store_true",
                    help="replace an existing binding for the intent (deletes the old one "
                         "from Fuseki first; with --write-only nothing is deleted)")
    args = ap.parse_args(argv)
    try:
        out, loaded = bind(args.intent_id, args.place, abox_dir=args.abox_dir,
                           write_only=args.write_only, data_endpoint=args.endpoint,
                           query_endpoint=args.query_endpoint, replace=args.replace,
                           update_endpoint=args.update_endpoint)
    except LoadError as exc:
        if exc.removed:
            print(f"Wrote {exc.path}. The old binding was removed and the new one was not "
                  "accepted, so this intent currently has no binding (the broker will return "
                  "inconclusive, no binding for intent, until the file is loaded). Load the "
                  "file again with python -m ingestion.load_ontologies.", file=sys.stderr)
        elif exc.delete_failed:
            print(f"Wrote {exc.path} but the old binding could not be deleted. Nothing was "
                  "deleted or loaded; the old binding is unchanged.", file=sys.stderr)
        else:
            print(f"Wrote {exc.path} but the network graph did not accept it. "
                  "Nothing was loaded; fix the cause and load the file again.", file=sys.stderr)
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
    if loaded:
        print(f"Wrote {out} and loaded it into the network graph.")
    else:
        print(f"Wrote {out}. It was not loaded (--write-only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
