"""Query endpoint of the broker's network graph in Fuseki (service bindings and topology).

This is not the TMF921 server's graph; the two are separate."""
import os

FUSEKI_QUERY_ENDPOINT = os.environ.get(
    "FUSEKI_QUERY_ENDPOINT", "http://localhost:3030/network-dataset/query"
)
