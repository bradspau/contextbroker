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

    def __init__(self, path):
        super().__init__("load failed")
        self.path = path


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
        check_uri(uri)
        if uri not in placements:
            raise BindError(f"no placement for {_short_name(uri)}")
        iri = interface_iris.get(placements[uri])
        if iri is None:
            raise BindError(f"no interface for {_short_name(uri)}")
        check_uri(iri, "interface iri")
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
    if "/" in device:
        raise BindError(f"unsafe value in --place: {spec!r}")
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
    rows = resp.json()["results"]["bindings"]
    iris = {r["i"]["value"] for r in rows if r.get("i", {}).get("type") == "uri"}
    if not iris:
        raise BindError(f"interface {device}/{iface} is not in the topology")
    if len(iris) > 1:
        raise BindError(f"interface {device}/{iface} is ambiguous in the topology")
    return iris.pop()


def bind(intent_id, place_specs, *, abox_dir=DEFAULT_ABOX_DIR, write_only=False,
         query_endpoint=FUSEKI_QUERY_ENDPOINT, data_endpoint=DEFAULT_DATA_ENDPOINT,
         client=None) -> tuple[Path, bool]:
    """Returns (path written, loaded). Raises BindError before writing anything."""
    _check_intent_id(intent_id)
    bounds = intent_bounds((client or Tmf921Client()).get_intent(intent_id))
    if not bounds:
        raise BindError("the intent's bounds could not be read")
    mmap = load_metric_map()
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
            ok, _report = validate_shapes(probe, s)
            if not ok:
                raise BindError("the binding failed SHACL validation")

    out_dir = Path(abox_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{intent_id}.ttl"
    out.write_text(ttl, encoding="utf-8")
    if write_only:
        return out, False
    try:
        load_ttl_to_fuseki(out, data_endpoint)
    except requests.RequestException:
        raise LoadError(out) from None
    return out, True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("intent_id")
    ap.add_argument("--place", action="append", default=[],
                    metavar="METRIC=DEVICE/INTERFACE")
    ap.add_argument("--write-only", action="store_true")
    ap.add_argument("--abox-dir", type=Path, default=DEFAULT_ABOX_DIR)
    ap.add_argument("--endpoint", default=DEFAULT_DATA_ENDPOINT)
    args = ap.parse_args(argv)
    try:
        out, loaded = bind(args.intent_id, args.place, abox_dir=args.abox_dir,
                           write_only=args.write_only, data_endpoint=args.endpoint)
    except LoadError as exc:
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
