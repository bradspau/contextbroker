"""scripts/create_demo_intent.sh against an in-process fake intent server.

The live TMF921 server only evaluates observations for an ACTIVE intent (a new
intent is ACKNOWLEDGED), so the script must activate the intent it creates.
"""
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "create_demo_intent.sh"
INTENT_ID = "11111111-2222-3333-4444-555555555555"


def _server(patch_status=200, patch_lifecycle="ACTIVE", post_reply=None):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _reply(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            calls.append(("POST", self.path, self._body()))
            self._reply(201, post_reply if post_reply is not None else
                        {"id": INTENT_ID, "lifecycleStatus": "ACKNOWLEDGED"})

        def do_PATCH(self):
            calls.append(("PATCH", self.path, self._body()))
            self._reply(patch_status, {"id": INTENT_ID, "lifecycleStatus": patch_lifecycle})

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, calls


def _run(srv, ttl):
    base = f"http://127.0.0.1:{srv.server_address[1]}/tmf-api/intentManagement/v5"
    return subprocess.run(["bash", str(SCRIPT), str(ttl), "Demo"], capture_output=True,
                          text=True, env={"PATH": "/usr/bin:/bin", "TMF921_BASE_URL": base},
                          timeout=30)


@pytest.fixture
def ttl(tmp_path):
    p = tmp_path / "intent.ttl"
    p.write_text("@prefix ex: <http://example.org/> .\nex:a ex:b ex:c .\n")
    return p


def test_script_creates_then_activates_the_intent(ttl):
    srv, calls = _server()
    try:
        out = _run(srv, ttl)
    finally:
        srv.shutdown()
    assert out.returncode == 0, out.stderr
    assert f"Created intent id: {INTENT_ID}" in out.stdout
    assert [c[0] for c in calls] == ["POST", "PATCH"]
    assert calls[0][1].endswith("/intent")
    assert calls[0][2]["expression"]["expressionValue"].startswith("@prefix ex:")
    assert calls[1][1].endswith(f"/intent/{INTENT_ID}")
    assert calls[1][2] == {"lifecycleStatus": "ACTIVE"}


@pytest.mark.parametrize("status,lifecycle", [(500, "ACTIVE"), (200, "ACKNOWLEDGED")])
def test_script_fails_when_activation_does_not_take(ttl, status, lifecycle):
    srv, _calls = _server(patch_status=status, patch_lifecycle=lifecycle)
    try:
        out = _run(srv, ttl)
    finally:
        srv.shutdown()
    assert out.returncode != 0
    # The id is still printed on stderr so the half-made intent can be found.
    assert INTENT_ID in out.stderr


@pytest.mark.parametrize("reply", [
    {"lifecycleStatus": "ACKNOWLEDGED", "note": "SERVERLEAK"},
    {"id": "../x", "note": "SERVERLEAK"},
    {"id": "a b", "note": "SERVERLEAK"},
    {"id": "a'b\"c", "note": "SERVERLEAK"},
    {"id": 7, "note": "SERVERLEAK"},
])
def test_script_refuses_missing_or_unsafe_id(ttl, reply):
    srv, calls = _server(post_reply=reply)
    try:
        out = _run(srv, ttl)
    finally:
        srv.shutdown()
    assert out.returncode != 0
    assert [c[0] for c in calls] == ["POST"]  # no PATCH
    assert "SERVERLEAK" not in out.stdout + out.stderr
    assert "../x" not in out.stdout + out.stderr
