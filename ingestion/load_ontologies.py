"""SHACL-gated loader: validates TBox/ABox files, then POSTs them to Fuseki."""
import argparse
import pathlib
from pathlib import Path

import rdflib
from pyshacl import validate

from seed_graph import DEFAULT_DATA_ENDPOINT, load_ttl_to_fuseki

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TBOX_DIR = REPO_ROOT / "ontologies" / "tbox"
DEFAULT_SHACL_DIR = REPO_ROOT / "ontologies" / "shacl"


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
) -> None:
    files = sorted(Path(tbox_dir).glob("*.ttl"))
    shapes_files = sorted(Path(shacl_dir).glob("*.ttl"))
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
    ap.add_argument("--endpoint", default=DEFAULT_DATA_ENDPOINT)
    args = ap.parse_args()
    load_ontologies(args.tbox_dir, args.shacl_dir, args.endpoint)
    print(f"Loaded {args.tbox_dir} into {args.endpoint}")


if __name__ == "__main__":
    main()
