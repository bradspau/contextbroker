"""SHACL-gated loader: validates TBox/ABox files, then POSTs them to Fuseki.

Only allow-listed TBox files load. An ABox file loads only if its name is on
ABOX_STATIC_ALLOWLIST (the tracked demo topology) or its first line is ABOX_MARKER, which
ingestion.bind_intent writes; any other file is refused before validation and before any POST."""
import argparse
import pathlib
from pathlib import Path

import requests
import rdflib
from pyshacl import validate

from broker_engine.sparql_resolver import FUSEKI_PASSWORD_ENV, fuseki_auth
from broker_engine.validation import REQUEST_TIMEOUT_S

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TBOX_DIR = REPO_ROOT / "ontologies" / "tbox"
DEFAULT_SHACL_DIR = REPO_ROOT / "ontologies" / "shacl"
DEFAULT_ABOX_DIR = REPO_ROOT / "ontologies" / "abox"
DEFAULT_TTL_PATH = DEFAULT_TBOX_DIR / "topology.ttl"
DEFAULT_DATA_ENDPOINT = "http://localhost:3030/network-dataset/data"
# Only our own TBox loads; licensed ontology files are refused even if copied into the directory.
TBOX_ALLOWLIST = ("topology.ttl",)
# The first line of every ABox file ingestion.bind_intent writes. An ABox file loads only when its
# first line is this marker or its name is on ABOX_STATIC_ALLOWLIST.
ABOX_MARKER = "# written by ingestion.bind_intent"
# Tracked demo data. Every other ABox file must have been written by bind_intent and start with
# ABOX_MARKER, so a licensed or stale .ttl dropped into the directory is never loaded.
ABOX_STATIC_ALLOWLIST = ("topology-demo.ttl",)


class MissingCredential(SystemExit):
    """FUSEKI_ADMIN_PASSWORD is not set. The message names the variable, never a value."""


def fuseki_credentials() -> tuple[str, str]:
    """The Fuseki credential from the environment, or exit naming the variable."""
    auth = fuseki_auth()
    if auth is None:
        raise MissingCredential(
            f"{FUSEKI_PASSWORD_ENV} is not set; set it in the environment (there is no default)"
        )
    return auth


def load_ttl_to_fuseki(
    ttl_path: Path,
    data_endpoint: str,
    auth: tuple[str, str] | None = None,
) -> requests.Response:
    if auth is None:
        auth = fuseki_credentials()
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


def _has_abox_marker(path: Path) -> bool:
    """True when the first line of the file is exactly ABOX_MARKER. Reads one short line only."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline(len(ABOX_MARKER) + 3)
    except OSError:
        return False
    return first.rstrip("\r\n") == ABOX_MARKER


def _abox_files(abox_dir: Path) -> list[Path]:
    """The ABox files that may load; raises, naming the files only, for any that may not."""
    found = sorted(Path(abox_dir).glob("*.ttl"))
    refused = sorted(
        f.name for f in found if f.name not in ABOX_STATIC_ALLOWLIST and not _has_abox_marker(f)
    )
    if refused:
        raise ValueError("ABox file(s) not written by the tooling; nothing loaded: " + ", ".join(refused))
    abox_real = Path(abox_dir).resolve()
    for f in found:
        if f.resolve().parent != abox_real:
            raise ValueError(f"ABox file links outside the ABox directory; nothing loaded: {f.name}")
    return found


def validate_shapes(data_ttl: Path, shapes_ttl: Path) -> tuple[bool, str]:
    data = rdflib.Graph().parse(str(data_ttl), format="turtle")
    shapes = rdflib.Graph().parse(str(shapes_ttl), format="turtle")
    conforms, _, report_text = validate(data, shacl_graph=shapes, inference="none")
    return bool(conforms), report_text


def load_ontologies(
    tbox_dir: Path,
    shacl_dir: Path,
    data_endpoint: str,
    auth: tuple[str, str] | None = None,
    abox_dir: Path | None = None,
    allow: tuple[str, ...] = TBOX_ALLOWLIST,
) -> None:
    if auth is None:
        auth = fuseki_credentials()
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
        files += _abox_files(abox_dir)
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
    fuseki_credentials()
    load_ontologies(args.tbox_dir, args.shacl_dir, args.endpoint, abox_dir=args.abox_dir)
    print(f"Loaded {args.tbox_dir} into {args.endpoint}")


if __name__ == "__main__":
    main()
