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
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8081"))
DEFAULT_JSONP_CALLBACK = "_jqjsp"
# Only allow valid JS identifiers as the callback name — it gets embedded
# directly in the response as executable JavaScript.
_SAFE_CALLBACK = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


class JsonpProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        query = params.get("query", [None])[0]
        callback = params.get("callback", [DEFAULT_JSONP_CALLBACK])[0]

        if not query:
            self.send_error(400, "Missing 'query' parameter")
            return
        if not _SAFE_CALLBACK.match(callback):
            self.send_error(400, f"Unsafe 'callback' parameter: {callback!r}")
            return

        try:
            response = requests.get(
                FUSEKI_QUERY_ENDPOINT,
                params={"query": query},
                headers={"Accept": "application/sparql-results+json"},
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            self.send_error(502, f"Upstream Fuseki request failed: {exc}")
            return

        body = f"{callback}({response.text});".encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - matches base signature
        print(f"{self.address_string()} - {format % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), JsonpProxyHandler)
    print(f"lodlive-proxy listening on :{LISTEN_PORT}, forwarding to {FUSEKI_QUERY_ENDPOINT}")
    server.serve_forever()
