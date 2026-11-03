"""scripts/check_fuseki_auth.sh against an in-process fake Fuseki.

The script must tell a refused unauthenticated update (exit 0) from an accepted
one (exit 1), treat every other answer as unexpected (exit 2), send no
credential, and never print a response body.
"""
import os
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("curl") is None,
                                reason="curl is not installed; the script needs it")

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_fuseki_auth.sh"
CANARY = "SERVERLEAK-CANARY"


def _server(status):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            calls.append({"path": self.path, "headers": dict(self.headers.items()),
                          "body": self.rfile.read(n).decode()})
            data = CANARY.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, calls


def _run(url):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    return subprocess.run(["bash", str(SCRIPT), url], capture_output=True, text=True,
                          env=env, timeout=30)


@pytest.mark.parametrize("status,code,word", [
    (401, 0, "refused (401)"),
    (403, 0, "refused (403)"),
    (204, 1, "ACCEPTED (204)"),
    (200, 1, "ACCEPTED (200)"),
    (500, 2, "unexpected (500)"),
    (302, 2, "unexpected (302)"),
])
def test_exit_code_follows_the_status_class(status, code, word):
    srv, _calls = _server(status)
    try:
        out = _run(f"http://127.0.0.1:{srv.server_address[1]}/network-dataset")
    finally:
        srv.shutdown()
    assert out.returncode == code, out.stdout + out.stderr
    assert out.stdout.strip() == f"unauthenticated update: {word}"


def test_closed_port_is_unexpected_not_refused():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    out = _run(f"http://127.0.0.1:{port}/network-dataset")
    assert out.returncode == 2
    assert out.stdout.strip() == "unauthenticated update: unexpected (000)"


def test_request_carries_no_credential_and_is_the_noop_update():
    srv, calls = _server(401)
    try:
        _run(f"http://127.0.0.1:{srv.server_address[1]}/network-dataset")
    finally:
        srv.shutdown()
    assert len(calls) == 1
    call = calls[0]
    assert call["path"] == "/network-dataset/update"
    assert "Authorization" not in call["headers"]
    assert call["headers"]["Content-Type"] == "application/sparql-update"
    assert call["body"] == "DELETE WHERE { <urn:contextbroker:auth-check> ?p ?o }"


@pytest.mark.parametrize("status", [200, 204, 401, 403, 500])
def test_response_body_is_never_printed(status):
    srv, _calls = _server(status)
    try:
        out = _run(f"http://127.0.0.1:{srv.server_address[1]}/network-dataset")
    finally:
        srv.shutdown()
    assert CANARY not in out.stdout + out.stderr


def test_trailing_slash_in_the_url_is_tolerated():
    srv, calls = _server(401)
    try:
        out = _run(f"http://127.0.0.1:{srv.server_address[1]}/network-dataset/")
    finally:
        srv.shutdown()
    assert out.returncode == 0
    assert calls[0]["path"] == "/network-dataset/update"
