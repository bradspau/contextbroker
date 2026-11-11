"""The LodLive JSONP proxy: credential handling, the read-only query filter and fixed replies.

The proxy forwards a query only when its first keyword is SELECT, ASK, DESCRIBE or CONSTRUCT and
no SERVICE or update keyword appears outside comments, string literals and IRIs. Every refusal is
one fixed 400 that echoes nothing from the query. The callback 400 and the upstream 502 are fixed
texts too. The credential and the CORS header are bead contextbroker-7i2 and are not tested here.

The proxy is its own image and imports nothing from the broker, so it is loaded from its path.
"""
import importlib.util
import re
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


# --- the read-only filter ------------------------------------------------------------------

ALLOWED = [
    "SELECT * WHERE {?s ?p ?o}",
    "select * where {?a ?b ?c} LIMIT 1",
    "SeLeCt * WhErE {?s ?p ?o}",
    "ASK { ?s ?p ?o }",
    "ask { ?s ?p ?o }",
    "DESCRIBE <http://x/a>",
    "describe ?s WHERE { ?s ?p ?o }",
    "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
    "construct { ?s ?p ?o } where { ?s ?p ?o }",
    "PREFIX ex: <http://x/> SELECT * WHERE { ex:a ?p ?o }",
    "PREFIX ex: <http://x/>\nPREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
    "SELECT * WHERE { ex:a rdf:type ?o }",
    "BASE <http://x/> SELECT * WHERE { <a> ?p ?o }",
    "BASE <http://x/> PREFIX ex: <http://x/> SELECT * WHERE { ex:a ?p ?o }",
    "# a comment line\nSELECT * WHERE { ?s ?p ?o }",
    "SELECT * WHERE { ?s ?p ?o } # trailing DROP ALL",
    "SELECT * FROM <http://x/g> WHERE { ?s ?p ?o }",
    "SELECT * FROM <http://x/g> FROM NAMED <http://x/h> WHERE { GRAPH ?g { ?s ?p ?o } }",
    "SELECT * WHERE { { SELECT ?s WHERE { ?s ?p ?o } LIMIT 5 } ?s ?q ?r }",
    'SELECT * WHERE { ?s ?p "SERVICE" }',
    "SELECT * WHERE { ?s ?p 'DROP GRAPH <http://x/g>' }",
    'SELECT * WHERE { ?s ?p """a long\nSERVICE <http://x/> string""" }',
    "SELECT * WHERE { ?s ?p '''INSERT DATA {}''' }",
    "SELECT * WHERE { ?s ?p ?o } # SERVICE <http://x/> {}",
    "SELECT * WHERE { <http://x/#SERVICE> ?p ?o }",
    "SELECT * WHERE { ?service ?p ?o }",
    "PREFIX : <http://x/> SELECT * WHERE { ?s :service ?o }",
    "SELECT * WHERE { ?s ?p ?o . FILTER(?o < 5 && ?o > 1) }",
    "SELECT * WHERE { ?s ?p ?o . FILTER(?o <= 5) }",
    "SELECT * WHERE { ?s ?p ?o . FILTER(regex(str(?o), 'x', 'i')) } LIMIT 1",
]

REFUSED = [
    "INSERT DATA { <http://x/a> <http://x/b> <http://x/c> }",
    "DELETE DATA { <http://x/a> <http://x/b> <http://x/c> }",
    "DELETE WHERE { ?s ?p ?o }",
    "INSERT { ?s ?p ?o } WHERE { ?s ?p ?o }",
    "DELETE { ?s ?p ?o } INSERT { ?s ?p 1 } WHERE { ?s ?p ?o }",
    "WITH <http://x/g> DELETE { ?s ?p ?o } WHERE { ?s ?p ?o }",
    "LOAD <http://x/data>",
    "CLEAR ALL",
    "CREATE GRAPH <http://x/g>",
    "DROP GRAPH <http://x/g>",
    "COPY <http://x/a> TO <http://x/b>",
    "MOVE <http://x/a> TO <http://x/b>",
    "ADD <http://x/a> TO <http://x/b>",
    "SELECT * WHERE { SERVICE <http://x/> { ?s ?p ?o } }",
    "SELECT * WHERE { SERVICE SILENT <http://x/> { ?s ?p ?o } }",
    "SELECT * WHERE { SERVICE<http://x/> { ?s ?p ?o } }",
    "SELECT * WHERE { service <http://x/> { ?s ?p ?o } }",
    "SELECT * WHERE { SeRvIcE <http://x/> { ?s ?p ?o } }",
    "SELECT * WHERE { ?s ?p ?o . SERVICE ?e { ?s ?p ?o } }",
    "ASK { SERVICE <http://x/> { ?s ?p ?o } }",
    "SELECT * WHERE { ?s ?p ?o } ; DROP ALL",
    "SELECT * WHERE { ?s ?p ?o } ; INSERT DATA { <a> <b> <c> }",
    "SELECT * WHERE { ?s ?p ?o } SELECT * WHERE { ?s ?p ?o }; CLEAR ALL",
    'SELECT * WHERE { ?s ?p "unterminated }',
    "SELECT * WHERE { ?s ?p 'unterminated }",
    'SELECT * WHERE { ?s ?p """unterminated }',
    "SELECT * WHERE { ?s ?p 'line\nbreak' }",
    "",
    "   \n\t ",
    "PREFIX ex: <http://x/>",
    "PREFIX ex: <http://x/> BASE <http://x/>",
    "FROM <http://x/g>",
    "hello",
    "{ ?s ?p ?o }",
]

# Each of these holds a real backslash: raw strings, and a test asserts it.
ESCAPED = [
    r"SELECT * WHERE { \u0053ERVICE <http://x/> { ?s ?p ?o } }",
    r"SELECT * WHERE { \U00000053ERVICE <http://x/> { ?s ?p ?o } }",
    r"\u0049NSERT DATA { <a> <b> <c> }",
    r"SELECT * WHERE { ?s ?p ?o } ; \u0044ROP ALL",
    r'SELECT * WHERE { ?s ?p "x\u0022 } ; DROP ALL #" }',
]


@pytest.mark.parametrize("query", ALLOWED)
def test_read_only_forms_are_allowed(proxy_module, query):
    assert proxy_module.is_read_only(query) is True


@pytest.mark.parametrize("query", REFUSED)
def test_updates_service_and_malformed_queries_are_refused(proxy_module, query):
    assert proxy_module.is_read_only(query) is False


@pytest.mark.parametrize("query", ESCAPED)
def test_a_codepoint_escape_is_refused(proxy_module, query):
    assert chr(92) in query
    assert proxy_module.is_read_only(query) is False


def test_the_escape_alone_refuses_a_query_and_its_plain_twin_passes(proxy_module):
    with_escape = r'SELECT * WHERE { ?s ?p "caf\u00e9" }'
    plain = 'SELECT * WHERE { ?s ?p "cafe" }'
    assert chr(92) in with_escape and chr(92) not in plain
    assert proxy_module.is_read_only(with_escape) is False
    assert proxy_module.is_read_only(plain) is True


@pytest.mark.parametrize("value", [None, 5, b"SELECT * WHERE {?s ?p ?o}", ["SELECT * WHERE {}"]])
def test_a_non_string_is_refused(proxy_module, value):
    assert proxy_module.is_read_only(value) is False


def test_the_named_sets_are_the_pinned_ones(proxy_module):
    assert proxy_module.READ_FORMS == {"SELECT", "ASK", "DESCRIBE", "CONSTRUCT"}
    assert proxy_module.FORBIDDEN_KEYWORDS == {
        "SERVICE", "INSERT", "DELETE", "LOAD", "CLEAR", "CREATE", "DROP",
        "COPY", "MOVE", "ADD", "WITH", "USING"}
    assert proxy_module.REFUSED_QUERY == "Only read-only SPARQL queries are accepted"
    assert proxy_module.UNSAFE_CALLBACK == "Unsafe 'callback' parameter"
    assert proxy_module.UPSTREAM_FAILED == "Upstream Fuseki request failed"


# --- the fixed replies over HTTP -----------------------------------------------------------

def _error_body(base, query=None, callback="lodlive"):
    params = {"callback": callback}
    if query is not None:
        params["query"] = query
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/?" + urllib.parse.urlencode(params), timeout=10)
    return exc.value.code, exc.value.read().decode("utf-8", "replace")


def test_every_refused_query_gets_the_same_400_with_no_call_to_fuseki(proxy_module, server):
    bodies = set()
    queries = ["INSERT DATA { <http://x/a> <http://x/b> <http://x/c> }",
               "SELECT * WHERE { SERVICE <http://secret.example/zz> { ?s ?p ?o } }",
               r'SELECT * WHERE { ?s ?p "caf\u00e9" }',
               'SELECT * WHERE { ?s ?p "unterminated }',
               "hello CANARYTEXT"]
    with patch.object(requests, "get") as get:
        for query in queries:
            code, body = _error_body(server, query)
            assert code == 400
            assert proxy_module.REFUSED_QUERY in body
            for part in ("secret.example", "CANARYTEXT", "INSERT", "unterminated", "caf"):
                assert part not in body
            bodies.add(re.sub(r"\s+", " ", body))
    get.assert_not_called()
    assert len(bodies) == 1


def test_an_unsafe_callback_gets_a_fixed_400_that_does_not_echo_it(proxy_module, server):
    with patch.object(requests, "get") as get:
        code, body = _error_body(server, "SELECT * WHERE {?s ?p ?o}", callback="evil(1);CANARYCB")
    assert code == 400
    assert proxy_module.UNSAFE_CALLBACK in body
    assert "CANARYCB" not in body and "evil" not in body
    get.assert_not_called()


def test_an_upstream_failure_gets_a_fixed_502_that_does_not_echo_the_exception(
        proxy_module, server):
    err = requests.ConnectionError("http://fuseki.internal:3030 refused CANARYEXC")
    with patch.object(requests, "get", side_effect=err):
        code, body = _error_body(server, "SELECT * WHERE {?s ?p ?o}")
    assert code == 502
    assert proxy_module.UPSTREAM_FAILED in body
    assert "CANARYEXC" not in body and "fuseki.internal" not in body


def test_a_refused_query_is_checked_after_the_callback_and_before_the_call(proxy_module, server):
    with patch.object(requests, "get") as get:
        code, _ = _error_body(server, "DROP ALL", callback="not safe")
    assert code == 400
    get.assert_not_called()


# --- LodLive's own queries still pass ------------------------------------------------------

PROFILE = Path(__file__).resolve().parent.parent / "lodlive" / "lodlive.profile.js"
_SAMPLES = {"{URI}": "http://example.org/id/Router-A",
            "{CLASS}": "http://example.org/ont#Router",
            "{VALUE}": "Router-A"}


def _profile_templates():
    text = PROFILE.read_text()
    block = re.search(r"sparql\s*:\s*\{(.*?)\n\t\t\}", text, re.S).group(1)
    found = re.findall(r"(\w+)\s*:\s*'((?:[^'\\]|\\.)*)'", block)
    return {name: re.sub(r"\\(.)", r"\1", body) for name, body in found}


def test_the_profile_has_the_seven_templates():
    assert set(_profile_templates()) == {
        "allClasses", "findSubject", "documentUri", "document", "bnode", "inverse",
        "inverseSameAs"}


@pytest.mark.parametrize("name", ["allClasses", "findSubject", "documentUri", "document",
                                  "bnode", "inverse", "inverseSameAs"])
def test_each_lodlive_template_is_read_only_and_is_forwarded(proxy_module, server, name):
    query = _profile_templates()[name]
    for placeholder, value in _SAMPLES.items():
        query = query.replace(placeholder, value)
    assert "{URI}" not in query and "{CLASS}" not in query and "{VALUE}" not in query
    assert proxy_module.is_read_only(query) is True
    with patch.object(requests, "get", return_value=_upstream()) as get:
        status, _ = _call(server, query=query)
    assert status == 200
    assert get.call_count == 1
    assert get.call_args.kwargs["params"] == {"query": query}
