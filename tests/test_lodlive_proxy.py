"""First test of the LodLive JSONP proxy (its full hardening is HYG-01..03 in a later phase).

The proxy is its own image and imports nothing from the broker, so it is loaded from its path.
"""
import importlib.util
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

PROXY_PATH = Path(__file__).resolve().parent.parent / "lodlive-proxy" / "jsonp_proxy.py"
SECRET = "proxy0secret0value"


@pytest.fixture(scope="module")
def proxy_module():
    spec = importlib.util.spec_from_file_location("jsonp_proxy_under_test", PROXY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def server(proxy_module):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), proxy_module.JsonpProxyHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _upstream(text='{"head":{},"results":{"bindings":[]}}'):
    r = Mock()
    r.text = text
    r.raise_for_status = Mock()
    return r


def _call(base, query="SELECT * WHERE {?s ?p ?o}", callback="lodlive"):
    url = base + "/?" + urllib.parse.urlencode({"query": query, "callback": callback})
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_proxy_forwards_with_the_credential_when_set(proxy_module, server, monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", SECRET)
    with patch.object(requests, "get", return_value=_upstream()) as get:
        status, body = _call(server)
    assert status == 200
    assert body.startswith("lodlive(") and body.endswith(");")
    assert get.call_count == 1
    assert get.call_args.kwargs["auth"] == ("admin", SECRET)
    assert SECRET not in body


def test_proxy_forwards_without_auth_when_unset(proxy_module, server, monkeypatch):
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
    with patch.object(requests, "get", return_value=_upstream()) as get:
        status, body = _call(server)
    assert status == 200
    assert get.call_args.kwargs["auth"] is None


def test_proxy_reads_the_environment_on_each_request(proxy_module, server, monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "first0value")
    with patch.object(requests, "get", return_value=_upstream()) as get:
        _call(server)
        monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "second0value")
        _call(server)
    assert [c.kwargs["auth"] for c in get.call_args_list] == [
        ("admin", "first0value"), ("admin", "second0value")]


def test_credential_never_appears_in_a_failure_response_or_the_log(
        proxy_module, server, monkeypatch, capsys):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", SECRET)
    with patch.object(requests, "get", side_effect=requests.ConnectionError("down")):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _call(server)
    assert exc.value.code == 502
    body = exc.value.read().decode("utf-8", "replace")
    assert SECRET not in body
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


def test_proxy_still_refuses_a_missing_query_without_calling_fuseki(proxy_module, server):
    with patch.object(requests, "get") as get:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(server + "/?callback=lodlive", timeout=10)
    assert exc.value.code == 400
    get.assert_not_called()


def test_proxy_imports_nothing_from_the_broker():
    text = PROXY_PATH.read_text()
    for name in ("broker_engine", "ingestion", "intent_management"):
        assert f"import {name}" not in text and f"from {name}" not in text
