"""Query endpoint of the broker's network graph in Fuseki (service bindings and topology).

This is not the TMF921 server's graph; the two are separate."""
import os

FUSEKI_QUERY_ENDPOINT = os.environ.get(
    "FUSEKI_QUERY_ENDPOINT", "http://localhost:3030/network-dataset/query"
)

FUSEKI_USER = "admin"
FUSEKI_PASSWORD_ENV = "FUSEKI_ADMIN_PASSWORD"
FUSEKI_READER_USER = "reader"
FUSEKI_READER_PASSWORD_ENV = "FUSEKI_READER_PASSWORD"


def fuseki_write_auth() -> tuple[str, str] | None:
    """The admin pair from FUSEKI_ADMIN_PASSWORD, or None when it is unset or empty.

    Only the operator tools that change the network graph (the loader, the binder, the seed script)
    use it. Fuseki lets only this user update or use the graph store endpoint (docker/fuseki/config.ttl).
    Read on every call, never at import, so a rotated value takes effect."""
    value = os.environ.get(FUSEKI_PASSWORD_ENV, "")
    return (FUSEKI_USER, value) if value else None


def fuseki_auth() -> tuple[str, str] | None:
    """The Fuseki pair for reading: the query-only user when FUSEKI_READER_PASSWORD is set, else the
    admin pair, else None.

    The API and the LodLive proxy are given only the reader variable, so their credential cannot update
    the graph. The admin fallback is for an operator shell that holds only the admin variable. Read on
    every call, never at import. There is no default: the credential exists only if the operator set it."""
    reader = os.environ.get(FUSEKI_READER_PASSWORD_ENV, "")
    if reader:
        return (FUSEKI_READER_USER, reader)
    return fuseki_write_auth()
