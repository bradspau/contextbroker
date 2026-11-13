"""scripts/check_fuseki_reader.sh against an in-process fake Fuseki (bead contextbroker-oiq).

The script must tell a refused update from an accepted one, expect the query to be answered, send the
reader user and nothing else, keep the password off the command line, and never print a response body.
"""
import base64
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("curl") is None,
                                reason="curl is not installed; the script needs it")

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_fuseki_reader.sh"
CANARY = "SERVERLEAK-CANARY"
PASSWORD = "reader0check0value"


def _server(update_status, query_status):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _answer(self, status):
            calls.append({"method": self.command, "path": self.path.split("?")[0],
                          "authorization": self.headers.get("Authorization")})
            data = CANARY.encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._answer(update_status)

        def do_GET(self):
            self._answer(query_status)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, calls


def _run(url, password=PASSWORD):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if password is not None:
        env["FUSEKI_READER_PASSWORD"] = password
    return subprocess.run(["bash", str(SCRIPT), url], capture_output=True, text=True,
                          env=env, timeout=30)


@pytest.mark.parametrize("update,query,code,lines", [
    (403, 200, 0, ["reader update: refused (403)", "reader query: answered (200)"]),
    (401, 200, 0, ["reader update: refused (401)", "reader query: answered (200)"]),
    (204, 200, 1, ["reader update: ACCEPTED (204)", "reader query: answered (200)"]),
    (200, 403, 1, ["reader update: ACCEPTED (200)", "reader query: unexpected (403)"]),
    (403, 401, 2, ["reader update: refused (403)", "reader query: unexpected (401)"]),
    (500, 200, 2, ["reader update: unexpected (500)", "reader query: answered (200)"]),
])
def test_exit_code_and_lines_follow_the_two_statuses(update, query, code, lines):
    srv, _calls = _server(update, query)
    try:
        out = _run(f"http://127.0.0.1:{srv.server_address[1]}/network-dataset")
    finally:
        srv.shutdown()
    assert out.returncode == code, out.stdout + out.stderr
    assert out.stdout.strip().splitlines() == lines
    assert CANARY not in out.stdout + out.stderr
    assert PASSWORD not in out.stdout + out.stderr


def test_it_sends_the_reader_user_to_the_update_and_query_endpoints_only():
    srv, calls = _server(403, 200)
    try:
        _run(f"http://127.0.0.1:{srv.server_address[1]}/network-dataset")
    finally:
        srv.shutdown()
    assert [(c["method"], c["path"]) for c in calls] == [
        ("POST", "/network-dataset/update"), ("GET", "/network-dataset/query")]
    expected = "Basic " + base64.b64encode(f"reader:{PASSWORD}".encode()).decode()
    assert {c["authorization"] for c in calls} == {expected}


def test_without_the_variable_it_sends_nothing_and_says_so():
    srv, calls = _server(403, 200)
    try:
        out = _run(f"http://127.0.0.1:{srv.server_address[1]}/network-dataset", password=None)
    finally:
        srv.shutdown()
    assert out.returncode == 2
    assert out.stdout.strip() == "reader update: unexpected (FUSEKI_READER_PASSWORD is not set)"
    assert calls == []


def test_the_password_is_never_on_a_command_line():
    text = SCRIPT.read_text()
    assert "-u " not in text and "--user" not in text
    assert "-K -" in text
