"""Loads ontology/topology.ttl into a Fuseki dataset via the Graph Store Protocol."""
import pathlib
import sys

import requests

DEFAULT_TTL_PATH = pathlib.Path(__file__).resolve().parent / "ontology" / "topology.ttl"
DEFAULT_DATA_ENDPOINT = "http://localhost:3030/network-dataset/data"


def load_ttl_to_fuseki(
    ttl_path: pathlib.Path,
    data_endpoint: str,
    auth: tuple[str, str] | None = ("admin", "admin"),
) -> requests.Response:
    payload = pathlib.Path(ttl_path).read_bytes()
    response = requests.post(
        data_endpoint,
        data=payload,
        headers={"Content-Type": "text/turtle"},
        auth=auth,
    )
    response.raise_for_status()
    return response


if __name__ == "__main__":
    ttl_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TTL_PATH
    endpoint = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DATA_ENDPOINT

    resp = load_ttl_to_fuseki(ttl_path, endpoint)
    print(f"Loaded {ttl_path} into {endpoint} — HTTP {resp.status_code}")
