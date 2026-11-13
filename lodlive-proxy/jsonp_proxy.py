"""JSONP-wrapping proxy in front of Fuseki, for LodLive.

LodLive's SPARQL requests go through jQuery's jquery.jsonp plugin, which
loads results via a <script src="..."> tag and expects the response body to
be JavaScript that calls a callback function — not raw JSON. Fuseki only
ever returns plain `application/sparql-results+json`, so without this
proxy the browser tries to execute raw JSON as a <script> and fails
silently.

The callback function name isn't fixed: lodlive.core.js calls
`$.jsonp.setup({callbackParameter: 'callback', callback: 'lodlive'})`
globally, so real requests carry an explicit `callback=lodlive` parameter
— but the plugin's own undeclared default (used if a caller omits
`callbackParameter`) is the fixed string "_jqjsp". This proxy honors
whatever `callback` name the request actually asks for, falling back to
"_jqjsp" only when none is given, and wraps the JSON response as
`<callback>(<json>);`.
"""
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

FUSEKI_QUERY_ENDPOINT = os.environ.get(
    "FUSEKI_QUERY_ENDPOINT", "http://fuseki:3030/network-dataset/query"
)
# The proxy holds the query-only Fuseki user: it can read the graph and cannot update it (bead
# contextbroker-oiq). It is never given the admin variable.
FUSEKI_USER = "reader"
FUSEKI_PASSWORD_ENV = "FUSEKI_READER_PASSWORD"
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8081"))
DEFAULT_JSONP_CALLBACK = "_jqjsp"
# Only allow valid JS identifiers as the callback name — it gets embedded
# directly in the response as executable JavaScript.
_SAFE_CALLBACK = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

# Replies are fixed text: nothing from the query, the callback or an upstream error is echoed.
REFUSED_QUERY = "Only read-only SPARQL queries are accepted"
UNSAFE_CALLBACK = "Unsafe 'callback' parameter"
UPSTREAM_FAILED = "Upstream Fuseki request failed"
ORIGIN_REFUSED = "Request not from the LodLive page"

# The pages allowed to ask. LodLive loads results through a <script> tag, which no CORS header governs,
# so any web page in the browser could otherwise read the graph through this proxy. A browser always
# sends the page's origin in Origin or Referer and a page cannot forge either, so a request is answered
# only when one of them names an allowed origin; a request that sends neither is refused. The default is
# the LodLive container as published on this machine (docker-compose.yml, port 8888).
DEFAULT_ALLOWED_ORIGINS = "http://localhost:8888,http://127.0.0.1:8888"
ALLOWED_ORIGINS_ENV = "LODLIVE_ALLOWED_ORIGINS"

# The read-only filter. A query is forwarded only when, after comments, string literals and
# IRIs are removed, its first keyword after any PREFIX and BASE declarations is a query form
# and no SERVICE or update keyword appears anywhere. FROM and FROM NAMED stay allowed.
READ_FORMS = frozenset({"SELECT", "ASK", "DESCRIBE", "CONSTRUCT"})
FORBIDDEN_KEYWORDS = frozenset({
    "SERVICE", "INSERT", "DELETE", "LOAD", "CLEAR", "CREATE", "DROP",
    "COPY", "MOVE", "ADD", "WITH", "USING",
})
# SPARQL IRIREF: no space and none of < > " { } | ^ ` \
_IRI = re.compile(r'<[^<>"{}|^`\\\x00-\x20]*>')
# SPARQL reads codepoint escapes before it tokenises, so a keyword can hide behind one.
_ESCAPE = re.compile(r"\\[uU][0-9A-Fa-f]")
# variable | prefixed name | word
_TOKEN = re.compile(r"[?$][A-Za-z0-9_]+|[A-Za-z0-9_.\-]*:[A-Za-z0-9_.:%\-]*|[A-Za-z]+")


def _strip(text):
    """Comments dropped, string literals and IRIs replaced by a placeholder; None if malformed."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            while i < n and text[i] not in "\r\n":
                i += 1
            out.append(" ")
        elif c in "\"'":
            long_form = text.startswith(c * 3, i)
            quote = c * 3 if long_form else c
            i += len(quote)
            while True:
                if i >= n:
                    return None  # unterminated literal
                if text[i] == "\\":
                    i += 2
                    continue
                if text.startswith(quote, i):
                    i += len(quote)
                    break
                if not long_form and text[i] in "\r\n":
                    return None
                i += 1
            out.append(" S ")
        elif c == "<":
            match = _IRI.match(text, i)  # a '<' that is not an IRIREF is an operator
            if match:
                out.append(" I ")
                i = match.end()
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def is_read_only(query) -> bool:
    """True only for a query that reads: see the filter rules above."""
    if not isinstance(query, str) or not query.strip() or _ESCAPE.search(query):
        return False
    stripped = _strip(query)
    if stripped is None:
        return False
    tokens = [t.upper() for t in _TOKEN.findall(stripped)]
    pos = 0
    while pos < len(tokens):  # PREFIX p: <iri>   and   BASE <iri>
        if tokens[pos] == "PREFIX":
            pos += 2
            if pos < len(tokens) and tokens[pos] == "I":
                pos += 1
        elif tokens[pos] == "BASE":
            pos += 1
            if pos < len(tokens) and tokens[pos] == "I":
                pos += 1
        else:
            break
    return (pos < len(tokens) and tokens[pos] in READ_FORMS
            and not any(t in FORBIDDEN_KEYWORDS for t in tokens))


def allowed_origins() -> frozenset[str]:
    """The allowed page origins from LODLIVE_ALLOWED_ORIGINS (comma separated), read on each call."""
    raw = os.environ.get(ALLOWED_ORIGINS_ENV) or DEFAULT_ALLOWED_ORIGINS
    return frozenset(o.strip().rstrip("/").lower() for o in raw.split(",") if o.strip())


def request_origin_allowed(origin, referer) -> bool:
    """True when the page's origin (Origin, else the origin part of Referer) is an allowed one."""
    seen = None
    if origin and origin != "null":
        seen = origin.strip().rstrip("/").lower()
    elif referer:
        parts = urllib.parse.urlsplit(referer.strip())
        if parts.scheme and parts.netloc:
            seen = f"{parts.scheme}://{parts.netloc}".lower()
    return seen is not None and seen in allowed_origins()


def fuseki_auth():
    """("reader", value) from FUSEKI_READER_PASSWORD, or None when it is unset or empty.

    Read on every call. The value is only ever passed as the requests auth argument;
    it is never logged or put in a response. Duplicated from the broker on purpose: this
    image imports nothing from it."""
    value = os.environ.get(FUSEKI_PASSWORD_ENV, "")
    return (FUSEKI_USER, value) if value else None


class JsonpProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not request_origin_allowed(self.headers.get("Origin"), self.headers.get("Referer")):
            self.send_error(403, ORIGIN_REFUSED)
            return
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        query = params.get("query", [None])[0]
        callback = params.get("callback", [DEFAULT_JSONP_CALLBACK])[0]

        if not query:
            self.send_error(400, "Missing 'query' parameter")
            return
        if not _SAFE_CALLBACK.match(callback):
            self.send_error(400, UNSAFE_CALLBACK)
            return
        if not is_read_only(query):
            self.send_error(400, REFUSED_QUERY)
            return

        try:
            response = requests.get(
                FUSEKI_QUERY_ENDPOINT,
                params={"query": query},
                headers={"Accept": "application/sparql-results+json"},
                auth=fuseki_auth(),
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException:
            self.send_error(502, UPSTREAM_FAILED)
            return

        body = f"{callback}({response.text});".encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Origin, Referer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - matches base signature
        print(f"{self.address_string()} - {format % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), JsonpProxyHandler)
    print(f"lodlive-proxy listening on :{LISTEN_PORT}, forwarding to {FUSEKI_QUERY_ENDPOINT}")
    server.serve_forever()
