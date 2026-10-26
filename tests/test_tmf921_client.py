"""Tmf921Client tests: no network; requests.get/post are patched."""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from broker_engine.validation import REQUEST_TIMEOUT_S, ClientInputError
from intent_management import tmf921_client as mod
from intent_management.tmf921_client import Tmf921Client, Tmf921Error, intent_bounds

FIX = Path(__file__).parent / "fixtures" / "tmf921"
URI = "http://broadband-forum.org/Intent#DownstreamBandwidthMetric"
BASE = "http://tmf-test.invalid:9/api/v5"
IID = "ffa9df3b-da2a-48ec-b029-4fec8c70531d"


def load(name):
    return json.loads((FIX / name).read_text())


def resp(status=200, body=None, text=None, json_exc=False):
    r = MagicMock()
    r.status_code = status
    r.ok = 200 <= status < 300
    if json_exc:
        r.json.side_effect = ValueError("SECRET-BODY")
    else:
        r.json.return_value = body
    r.text = text if text is not None else "SECRET-BODY"
    return r


@pytest.fixture
def client():
    return Tmf921Client(BASE)


# ---------- get / list ----------
def test_get_intent_happy(client):
    with patch("requests.get", return_value=resp(200, load("intent.json"))) as g:
        out = client.get_intent(IID)
    assert out["id"] == IID
    assert g.call_args.args[0] == f"{BASE}/intent/{IID}"
    assert g.call_args.kwargs["timeout"] == REQUEST_TIMEOUT_S


def test_list_reports_happy(client):
    body = [load("report_fulfilled.json"), load("report_degraded_violation.json")]
    with patch("requests.get", return_value=resp(200, body)) as g:
        out = client.list_reports(IID)
    assert out == body
    assert g.call_args.args[0] == f"{BASE}/intent/{IID}/intentReport"
    assert g.call_args.kwargs["timeout"] == REQUEST_TIMEOUT_S


def test_list_reports_non_list_is_error(client):
    with patch("requests.get", return_value=resp(200, {"a": 1})):
        with pytest.raises(Tmf921Error):
            client.list_reports(IID)


def test_get_intent_non_dict_is_error(client):
    with patch("requests.get", return_value=resp(200, [1])):
        with pytest.raises(Tmf921Error):
            client.get_intent(IID)


@pytest.mark.parametrize("bad", ["../x", "a b", "a;b", "", "a" * 65, "a/b", "id\n", "é"])
def test_unsafe_intent_id_makes_no_http_call(client, bad):
    with patch("requests.get") as g, patch("requests.post") as p:
        with pytest.raises(ClientInputError):
            client.get_intent(bad)
        with pytest.raises(ClientInputError):
            client.list_reports(bad)
        with pytest.raises(ClientInputError):
            client.post_observation(bad, URI, 1.0)
        with pytest.raises(ClientInputError):
            client.get_intent(None)
    assert g.call_count == 0 and p.call_count == 0


@pytest.mark.parametrize("r", [
    lambda: resp(404), lambda: resp(500), lambda: resp(200, json_exc=True),
])
def test_http_failures_are_generic(client, r):
    with patch("requests.get", return_value=r()):
        with pytest.raises(Tmf921Error) as ei:
            client.get_intent(IID)
    s = str(ei.value) + repr(ei.value.args)
    assert "SECRET-BODY" not in s and "tmf-test" not in s and IID not in s


def test_connection_error_is_generic(client):
    exc = requests.ConnectionError(f"HTTPConnectionPool {BASE} refused")
    with patch("requests.get", side_effect=exc):
        with pytest.raises(Tmf921Error) as ei:
            client.get_intent(IID)
    s = str(ei.value) + repr(ei.value.args)
    assert "tmf-test" not in s and "refused" not in s
    with patch("requests.post", side_effect=exc):
        with pytest.raises(Tmf921Error) as ei:
            client.post_observation(IID, URI, 1.0)
    assert "tmf-test" not in str(ei.value) + repr(ei.value.args)


# ---------- post_observation ----------
def test_post_observation_full_body(client):
    when = datetime(2026, 10, 26, 23, 30, 0, tzinfo=timezone.utc)
    with patch("requests.post", return_value=resp(201, {"echo": 1})) as p:
        assert client.post_observation(IID, URI, 150, "Mbps", when) is None
    assert p.call_args.args[0] == f"{BASE}/intent/{IID}/observation"
    assert p.call_args.kwargs["timeout"] == REQUEST_TIMEOUT_S
    body = p.call_args.kwargs["json"]
    assert body == {"metricUri": URI, "value": 150.0, "unit": "Mbps",
                    "obtainedAt": "2026-10-26T23:30:00Z"}
    assert body == {**load("observation_request.json"), "value": 150.0}


def test_post_observation_minimal_body_and_200(client):
    with patch("requests.post", return_value=resp(200, {})) as p:
        client.post_observation(IID, URI, 2.5)
    assert p.call_args.kwargs["json"] == {"metricUri": URI, "value": 2.5}


def test_post_observation_converts_offset_to_utc_z(client):
    tz = timezone(timedelta(hours=2))
    when = datetime(2026, 10, 27, 1, 30, 0, tzinfo=tz)
    with patch("requests.post", return_value=resp(201, {})) as p:
        client.post_observation(IID, URI, 1.0, None, when)
    assert p.call_args.kwargs["json"]["obtainedAt"] == "2026-10-26T23:30:00Z"


@pytest.mark.parametrize("uri", [
    "javascript:alert(1)", "http://x/y\n", "ftp://x", "", None, "http://a b", 5,
])
def test_post_observation_bad_uri(client, uri):
    with patch("requests.post") as p:
        with pytest.raises(ClientInputError):
            client.post_observation(IID, uri, 1.0)
    assert p.call_count == 0


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), -math.inf, "5", None])
def test_post_observation_bad_value(client, value):
    with patch("requests.post") as p:
        with pytest.raises(ClientInputError):
            client.post_observation(IID, URI, value)
    assert p.call_count == 0


@pytest.mark.parametrize("unit", ["furlongs", "Mbps\n", 5, ""])
def test_post_observation_bad_unit(client, unit):
    with patch("requests.post") as p:
        with pytest.raises(ClientInputError):
            client.post_observation(IID, URI, 1.0, unit)
    assert p.call_count == 0


@pytest.mark.parametrize("when", [datetime(2026, 1, 1), "2026-01-01T00:00:00Z", 5])
def test_post_observation_bad_time(client, when):
    with patch("requests.post") as p:
        with pytest.raises(ClientInputError):
            client.post_observation(IID, URI, 1.0, None, when)
    assert p.call_count == 0


@pytest.mark.parametrize("status", [400, 404, 500, 302])
def test_post_observation_non_success(client, status):
    with patch("requests.post", return_value=resp(status)):
        with pytest.raises(Tmf921Error) as ei:
            client.post_observation(IID, URI, 1.0)
    assert "SECRET-BODY" not in str(ei.value) + repr(ei.value.args)


# ---------- config ----------
def test_default_base_url_constant():
    assert mod.TMF921_BASE_URL.startswith("http")
    assert Tmf921Client().base_url == mod.TMF921_BASE_URL


def test_base_url_trailing_slash_stripped():
    assert Tmf921Client(BASE + "/").base_url == BASE


def test_error_has_no_url_even_with_cause(client):
    with patch("requests.get", return_value=resp(500)):
        with pytest.raises(Tmf921Error) as ei:
            client.get_intent(IID)
    assert BASE not in str(ei.value)
    assert all(BASE not in str(a) for a in ei.value.args)


# ---------- intent_bounds ----------
def _intent(ttl):
    return {"expression": {"@type": "TurtleExpression", "expressionValue": ttl}}


PRE = ("@prefix quan: <http://tio.models.tmforum.org/tio/v3.6.0/QuantityOntology/> .\n"
       "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
       "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n")


def cond(pred, uri, val, dtype="xsd:decimal", node="_:c"):
    return f'{node} quan:{pred} ( <{uri}> [ rdf:value "{val}"^^{dtype} ; quan:unit "Mbps" ] ) .\n'


def test_bounds_fixture():
    assert intent_bounds(load("intent.json")) == {URI: 100.0}


def test_bounds_rewritten_50():
    ttl = PRE + cond("atLeast", URI, "50")
    assert intent_bounds(_intent(ttl)) == {URI: 50.0}


def test_bounds_atmost_and_multiple_metrics():
    other = "http://broadband-forum.org/Intent#LatencyMetric"
    ttl = PRE + cond("atLeast", URI, "100") + cond("atMost", other, "20", "xsd:integer", "_:d")
    assert intent_bounds(_intent(ttl)) == {URI: 100.0, other: 20.0}


def test_bounds_same_metric_same_value_ok():
    ttl = PRE + cond("atLeast", URI, "100") + cond("atMost", URI, "100", node="_:d")
    assert intent_bounds(_intent(ttl)) == {URI: 100.0}


def test_bounds_same_metric_conflicting_is_empty():
    ttl = PRE + cond("atLeast", URI, "100") + cond("atMost", URI, "200", node="_:d")
    assert intent_bounds(_intent(ttl)) == {}


@pytest.mark.parametrize("bad", [
    {}, {"expression": None}, {"expression": 5}, {"expression": {}},
    {"expression": {"expressionValue": None}}, {"expression": {"expressionValue": 7}},
    {"expression": {"expressionValue": "this is ::: not turtle"}},
    None, [], "x", 5,
])
def test_bounds_unreadable(bad):
    assert intent_bounds(bad) == {}


@pytest.mark.parametrize("val,dt", [
    ("NaN", "xsd:double"), ("INF", "xsd:double"), ("-INF", "xsd:double"),
    ("true", "xsd:boolean"), ("abc", "xsd:string"),
])
def test_bounds_non_finite_or_non_numeric(val, dt):
    assert intent_bounds(_intent(PRE + cond("atLeast", URI, val, dt))) == {}


def test_bounds_non_finite_poisons_nothing_else_silently():
    other = "http://broadband-forum.org/Intent#LatencyMetric"
    ttl = PRE + cond("atLeast", URI, "NaN", "xsd:double") + cond("atMost", other, "20", node="_:d")
    assert intent_bounds(_intent(ttl)) == {}


def test_bounds_ignores_lists_of_wrong_shape():
    ttl = PRE + f'_:c quan:atLeast ( <{URI}> ) .\n_:d quan:x ( "a" "b" ) .\n'
    assert intent_bounds(_intent(ttl)) == {}


def test_bounds_makes_no_network_call():
    with patch("requests.get") as g, patch("requests.post") as p:
        intent_bounds(load("intent.json"))
    assert g.call_count == 0 and p.call_count == 0
