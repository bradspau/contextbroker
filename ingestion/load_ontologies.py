"""SHACL-gated loader: validates TBox/ABox files, then POSTs them to Fuseki."""
import argparse
import pathlib
from pathlib import Path

import requests
import rdflib
from pyshacl import validate

from broker_engine.validation import REQUEST_TIMEOUT_S

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TBOX_DIR = REPO_ROOT / "ontologies" / "tbox"
DEFAULT_SHACL_DIR = REPO_ROOT / "ontologies" / "shacl"
DEFAULT_ABOX_DIR = REPO_ROOT / "ontologies" / "abox"
DEFAULT_TTL_PATH = DEFAULT_TBOX_DIR / "topology.ttl"
DEFAULT_DATA_ENDPOINT = "http://localhost:3030/network-dataset/data"
# Only our own TBox loads; licensed ontology files are refused even if copied into the directory.
TBOX_ALLOWLIST = ("topology.ttl",)


def load_ttl_to_fuseki(
    ttl_path: Path,
    data_endpoint: str,
    auth: tuple[str, str] | None = ("admin", "admin"),
) -> requests.Response:
    payload = Path(ttl_path).read_bytes()
    response = requests.post(
        data_endpoint,
        data=payload,
        headers={"Content-Type": "text/turtle"},
        auth=auth,
        timeout=REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    return response


def validate_shapes(data_ttl: Path, shapes_ttl: Path) -> tuple[bool, str]:
    data = rdflib.Graph().parse(str(data_ttl), format="turtle")
    shapes = rdflib.Graph().parse(str(shapes_ttl), format="turtle")
    conforms, _, report_text = validate(data, shacl_graph=shapes, inference="none")
    return bool(conforms), report_text


def load_ontologies(
    tbox_dir: Path,
    shacl_dir: Path,
    data_endpoint: str,
    auth: tuple[str, str] | None = ("admin", "admin"),
    abox_dir: Path | None = None,
    allow: tuple[str, ...] = TBOX_ALLOWLIST,
) -> None:
    files = sorted(Path(tbox_dir).glob("*.ttl"))
    shapes_files = sorted(Path(shacl_dir).glob("*.ttl"))
    if not files:
        raise ValueError(f"no *.ttl files found in tbox_dir: {tbox_dir}")
    unlisted = sorted(f.name for f in files if f.name not in allow)
    if unlisted:
        raise ValueError("TBox file(s) not on the allow-list; nothing loaded: " + ", ".join(unlisted))
    tbox_real = Path(tbox_dir).resolve()
    for f in files:
        if f.resolve().parent != tbox_real:
            raise ValueError(f"TBox file links outside the TBox directory; nothing loaded: {f.name}")
    if abox_dir is not None and Path(abox_dir).is_dir():
        files += sorted(Path(abox_dir).glob("*.ttl"))
    if not shapes_files:
        raise ValueError(f"no *.ttl files found in shacl_dir: {shacl_dir}")
    failures = []
    for f in files:
        for shapes in shapes_files:
            ok, report = validate_shapes(f, shapes)
            if not ok:
                failures.append(f"{f.name} vs {shapes.name}:\n{report}")
    if failures:
        raise ValueError("SHACL validation failed; nothing loaded:\n" + "\n".join(failures))
    for f in files:
        load_ttl_to_fuseki(f, data_endpoint, auth)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tbox-dir", type=pathlib.Path, default=DEFAULT_TBOX_DIR)
    ap.add_argument("--shacl-dir", type=pathlib.Path, default=DEFAULT_SHACL_DIR)
    ap.add_argument("--abox-dir", type=pathlib.Path, default=DEFAULT_ABOX_DIR)
    ap.add_argument("--endpoint", default=DEFAULT_DATA_ENDPOINT)
    args = ap.parse_args()
    load_ontologies(args.tbox_dir, args.shacl_dir, args.endpoint, abox_dir=args.abox_dir)
    print(f"Loaded {args.tbox_dir} into {args.endpoint}")


if __name__ == "__main__":
    main()
