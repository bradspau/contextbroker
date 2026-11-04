"""Query endpoint of the broker's network graph in Fuseki (service bindings and topology).

This is not the TMF921 server's graph; the two are separate."""
import os

FUSEKI_QUERY_ENDPOINT = os.environ.get(
    "FUSEKI_QUERY_ENDPOINT", "http://localhost:3030/network-dataset/query"
)

FUSEKI_USER = "admin"
FUSEKI_PASSWORD_ENV = "FUSEKI_ADMIN_PASSWORD"


def fuseki_auth() -> tuple[str, str] | None:
    """The Fuseki basic-auth pair from the environment, or None when it is unset or empty.

    Read on every call, never at import, so a rotated value takes effect. There is no
    default: the credential exists only if the operator set FUSEKI_ADMIN_PASSWORD."""
    value = os.environ.get(FUSEKI_PASSWORD_ENV, "")
    return (FUSEKI_USER, value) if value else None
