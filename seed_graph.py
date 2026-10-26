"""Thin CLI shim: the loader now lives in ingestion.load_ontologies."""
import pathlib
import sys

import requests  # noqa: F401  (kept so tests can patch seed_graph.requests.post)

from ingestion.load_ontologies import (  # noqa: F401
    DEFAULT_DATA_ENDPOINT,
    DEFAULT_TTL_PATH,
    load_ttl_to_fuseki,
)

if __name__ == "__main__":
    ttl_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TTL_PATH
    endpoint = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DATA_ENDPOINT

    resp = load_ttl_to_fuseki(ttl_path, endpoint)
    print(f"Loaded {ttl_path} into {endpoint} — HTTP {resp.status_code}")
