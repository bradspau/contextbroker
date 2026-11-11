"""One reused HTTP session per upstream (PERF-01, D-71, D-87a).

Tests marked real_sessions talk to a loopback server over a real socket, so reuse,
retries, cookies and credentials are proved on the wire. Unmarked tests see the
autouse shim in conftest, which routes the helper to the patched requests functions.
"""
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest

from broker_engine import upstream_http
from broker_engine.tsdb_joiner import join_observed_metric


class _Loopback:
    """An HTTP/1.1 keep-alive server on 127.0.0.1 that records every request."""

    def __init__(self, responder=None):
        self.calls = []
        outer = self
        self.responder = responder or (lambda call: (200, {}, {}))

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                call = {"method": self.command, "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body, "client_port": self.client_address[1]}
                outer.calls.append(call)
                status, payload, extra = outer.responder(call)
                if status is None:
                    # Read the request, then drop the connection without answering.
                    self.close_connection = True
                    self.connection.close()
                    return
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            do_GET = _handle
            do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def ports(self):
        return {c["client_port"] for c in self.calls}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def loopback():
    made = []

    def make(responder=None):
        lb = _Loopback(responder)
        made.append(lb)
        return lb

    yield make
    upstream_http.reset_sessions()
    for lb in made:
        lb.close()


def _vm_payload(now):
    def responder(call):
        if "tlast_over_time" in call["path"] or "tlast_over_time" in call["body"]:
            value = now.timestamp() - 10
        else:
            value = 42.5
        return 200, {"data": {"result": [{"value": [now.timestamp(), str(value)]}]}}, {}
    return responder


def test_session_is_one_object_per_upstream_and_refuses_unknown_names():
    first = upstream_http.session("fuseki")
    assert first is upstream_http.session("fuseki")
    with pytest.raises(ValueError, match="unknown upstream"):
        upstream_http.session("qdrant")
    with pytest.raises(ValueError, match="unknown upstream"):
        upstream_http.session("")


@pytest.mark.real_sessions
def test_session_is_one_object_across_eight_threads():
    got = []
    barrier = threading.Barrier(8)

    def run():
        barrier.wait()
        got.append(upstream_http.session("victoriametrics"))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == 8 and all(s is got[0] for s in got)
    assert upstream_http.session("fuseki") is not got[0]
    assert upstream_http.session("tmf921") is not upstream_http.session("fuseki")


@pytest.mark.real_sessions
def test_reset_sessions_closes_and_forgets():
    old = upstream_http.session("fuseki")
    closed = []
    old.close = lambda: closed.append(True)
    upstream_http.reset_sessions()
    assert closed == [True]
    assert upstream_http.session("fuseki") is not old


@pytest.mark.real_sessions
def test_two_metric_joins_arrive_on_one_tcp_connection(loopback):
    now = datetime.now(timezone.utc)
    lb = loopback(_vm_payload(now))
    endpoint = f"{lb.url}/api/v1/query"
    envs = [join_observed_metric("Router-A", "Gi0", "latency", "5m",
                                 query_endpoint=endpoint, now=now) for _ in range(2)]
    assert len(lb.calls) == 4
    assert len(lb.ports()) == 1

    def fake_get(url, params=None, timeout=None):
        r = MagicMock()
        r.raise_for_status.return_value = None
        status, payload, _ = _vm_payload(now)({"path": params["query"], "body": ""})
        r.json.return_value = payload
        return r

    with patch("requests.get", side_effect=fake_get):
        expected = join_observed_metric("Router-A", "Gi0", "latency", "5m",
                                        query_endpoint=endpoint, now=now)
    assert envs[0].to_dict() == envs[1].to_dict() == expected.to_dict()


@pytest.mark.real_sessions
def test_sessions_never_retry():
    s = upstream_http.session("tmf921")
    for prefix in ("http://", "https://"):
        assert s.get_adapter(prefix + "x").max_retries.total == 0


@pytest.mark.real_sessions
def test_no_cookie_is_kept_and_no_credential_is_held(loopback):
    lb = loopback(lambda call: (200, {}, {"Set-Cookie": "sid=abc123; Path=/"}))
    upstream_http.get("tmf921", lb.url + "/a", timeout=5)
    upstream_http.get("tmf921", lb.url + "/b", timeout=5)
    assert len(lb.calls) == 2
    assert "cookie" not in lb.calls[1]["headers"]
    s = upstream_http.session("tmf921")
    assert s.auth is None
    assert "Authorization" not in s.headers


def test_shim_passes_keyword_arguments_to_the_patched_function_unchanged():
    with patch("requests.get") as g:
        upstream_http.get("victoriametrics", "http://x/q", params={"query": "q"}, timeout=10)
    g.assert_called_once_with("http://x/q", params={"query": "q"}, timeout=10)
    with patch("requests.post") as p:
        upstream_http.post("fuseki", "http://x/s", data="d", headers={"A": "b"}, timeout=3)
    p.assert_called_once_with("http://x/s", data="d", headers={"A": "b"}, timeout=3)


def test_shim_works_with_a_fake_that_has_a_fixed_signature():
    seen = []

    def fake(url, params=None, timeout=None):
        seen.append((url, params, timeout))
        return "ok"

    with patch("requests.get", fake):
        assert upstream_http.get("victoriametrics", "http://x", params={"a": 1},
                                 timeout=2) == "ok"
    assert seen == [("http://x", {"a": 1}, 2)]


def test_shim_still_refuses_an_unknown_upstream():
    with patch("requests.get") as g:
        with pytest.raises(ValueError, match="unknown upstream"):
            upstream_http.get("nope", "http://x")
    g.assert_not_called()


# ---- Fuseki and operations reads (plan 08-01 task 2) ---------------------------------


def _fuseki_empty(call):
    return 200, {"head": {"vars": []}, "results": {"bindings": []}}, {}


def _basic_credential(call):
    import base64

    header = call["headers"].get("authorization", "")
    assert header.startswith("Basic ")
    return base64.b64decode(header[6:]).decode()


@pytest.mark.real_sessions
def test_graph_reads_share_one_fuseki_connection_with_basic_auth(loopback, monkeypatch):
    from broker_engine.binding_resolver import fetch_fluents
    from broker_engine.context_graph import read_device_names
    from broker_engine.operations import interface_neighbours

    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "first0value")
    lb = loopback(_fuseki_empty)
    endpoint = f"{lb.url}/ds/query"
    interface_neighbours("Router-A", "Gi0", query_endpoint=endpoint)
    fetch_fluents("intent-1", query_endpoint=endpoint)
    read_device_names(query_endpoint=endpoint)
    assert len(lb.calls) == 3
    assert len(lb.ports()) == 1
    assert {_basic_credential(c) for c in lb.calls} == {"admin:first0value"}


@pytest.mark.real_sessions
def test_a_rotated_fuseki_credential_is_sent_on_the_next_call(loopback, monkeypatch):
    from broker_engine.operations import interface_neighbours

    lb = loopback(_fuseki_empty)
    endpoint = f"{lb.url}/ds/query"
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "first0value")
    interface_neighbours("Router-A", "Gi0", query_endpoint=endpoint)
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "second0value")
    interface_neighbours("Router-A", "Gi0", query_endpoint=endpoint)
    assert [_basic_credential(c) for c in lb.calls] == ["admin:first0value",
                                                         "admin:second0value"]
    assert upstream_http.session("fuseki").auth is None


@pytest.mark.real_sessions
def test_operations_and_a_join_reach_victoriametrics_on_one_connection(loopback):
    from broker_engine.operations import interfaces_with_nonzero

    now = datetime.now(timezone.utc)

    def responder(call):
        ts = now.timestamp() - 10 if "tlast_over_time" in call["path"] else 5
        series = {"metric": {"interface": "Gi0"}, "value": [now.timestamp(), str(ts)]}
        return 200, {"data": {"result": [series]}}, {}

    lb = loopback(responder)
    endpoint = f"{lb.url}/api/v1/query"
    out = interfaces_with_nonzero("Router-A", "latency", "5m", query_endpoint=endpoint, now=now)
    assert out["interfaces_examined"] == 1
    assert len(lb.calls) == 4
    join_observed_metric("Router-A", "Gi0", "latency", "5m", query_endpoint=endpoint, now=now)
    assert len(lb.calls) == 6
    assert len(lb.ports()) == 1


# ---- TMF921 client, health probes and the direct-call check (plan 08-01 task 3) ------

IID = "I1234abcd"
URI = "http://example.org/network-ontology#Latency"


def _tmf921_client(url):
    from intent_management.tmf921_client import Tmf921Client

    return Tmf921Client(base_url=url)


@pytest.mark.real_sessions
def test_tmf921_reads_arrive_on_one_tcp_connection(loopback):
    lb = loopback(lambda call: (200, {"id": IID}, {}))
    client = _tmf921_client(lb.url)
    assert client.get_intent(IID) == {"id": IID}
    assert client.get_intent(IID) == {"id": IID}
    assert len(lb.calls) == 2
    assert len(lb.ports()) == 1


@pytest.mark.real_sessions
def test_an_observation_post_whose_connection_drops_is_sent_exactly_once(loopback):
    from intent_management.tmf921_client import Tmf921Error

    lb = loopback(lambda call: (None, None, {}))
    client = _tmf921_client(lb.url)
    with pytest.raises(Tmf921Error):
        client.post_observation(IID, URI, 1.0)
    posts = [c for c in lb.calls if c["method"] == "POST"
             and c["path"] == f"/intent/{IID}/observation"]
    assert len(posts) == 1


@pytest.mark.real_sessions
def test_probes_answer_through_the_sessions_and_qdrant_stays_on_plain_requests(loopback):
    from broker_engine import health

    lb = loopback(lambda call: (200, {}, {}))
    assert health._probe("tmf921", lb.url + "/health", None) == health.ANSWERING
    assert health._probe("victoriametrics", lb.url + "/health", None) == health.ANSWERING
    assert len(lb.calls) == 2
    with patch("requests.get") as g:
        g.return_value.status_code = 200
        assert health._probe("qdrant", "http://127.0.0.1:1/healthz", None) == health.ANSWERING
    g.assert_called_once()


_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "request"}


def _direct_requests_calls():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    found, sessions = [], []
    for pkg in ("broker_engine", "intent_management"):
        for path in sorted((root / pkg).rglob("*.py")):
            tree = ast.parse(path.read_text())
            owner = {}
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for child in ast.walk(fn):
                        owner[id(child)] = fn.name
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "requests"):
                    continue
                where = (str(path.relative_to(root)), node.lineno, owner.get(id(node)))
                if node.func.attr in _HTTP_VERBS:
                    found.append(where)
                elif node.func.attr == "Session":
                    sessions.append(where)
    return found, sessions


def test_direct_requests_calls_are_only_the_qdrant_probe():
    found, _ = _direct_requests_calls()
    assert [(f, fn) for f, _, fn in found] == [("broker_engine/health.py", "_probe")], (
        f"a direct requests call outside the Qdrant probe (file, line, function): {found}")


def test_direct_session_construction_is_only_in_upstream_http():
    _, sessions = _direct_requests_calls()
    assert [(f, fn) for f, _, fn in sessions] == [("broker_engine/upstream_http.py",
                                                    "_new_session")], (
        f"requests.Session( outside upstream_http (file, line, function): {sessions}")
