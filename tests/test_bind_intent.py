from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import rdflib

from ingestion import bind_intent
from ingestion.bind_intent import BindError, main, render_binding, bind
from ingestion.load_ontologies import validate_shapes
from intent_management.tmf921_client import Tmf921Error

ROOT = Path(__file__).resolve().parent.parent
SHAPES = ROOT / "ontologies" / "shacl" / "topology.shacl.ttl"
LAT = "http://broadband-forum.org/Intent#LatencyMetric"
IFACE_IRI = "http://example.org/network-ontology#if-olt1-pon0"
INTENT = {"expression": {"expressionValue": (
    "@prefix quan: <http://tio.models.tmforum.org/tio/v3.6.0/QuantityOntology/> .\n"
    "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
    "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
    '_:c quan:smaller ( <' + LAT + '> [ rdf:value "25"^^xsd:decimal ] ) .\n')}}


def _sparql_reply(iris):
    r = Mock()
    r.raise_for_status = Mock()
    r.json.return_value = {"results": {"bindings": [
        {"i": {"type": "uri", "value": u}} for u in iris]}}
    return r


def _ok_post():
    r = Mock()
    r.raise_for_status = Mock()
    return r


def _run(tmp_path, args, *, iris=(IFACE_IRI,), intent=INTENT, client_exc=None):
    """Run main() with the TMF921 client and requests mocked."""
    client = Mock()
    if client_exc:
        client.get_intent.side_effect = client_exc
    else:
        client.get_intent.return_value = intent
    posts = []

    def fake_post(url, **kw):
        posts.append((url, kw))
        return _sparql_reply(iris) if url.endswith("/query") else _ok_post()

    with patch.object(bind_intent, "Tmf921Client", return_value=client), \
            patch("requests.post", side_effect=fake_post):
        code = main(["intent-1", "--abox-dir", str(tmp_path / "abox"), *args])
    return code, posts


def test_render_binding_roundtrips_through_the_shacl_shape(tmp_path):
    ttl = render_binding("intent-1", {LAT: 25.0}, {LAT: ("olt1", "pon0")},
                         {("olt1", "pon0"): IFACE_IRI})
    f = tmp_path / "b.ttl"
    f.write_text(ttl)
    ok, report = validate_shapes(f, SHAPES)
    assert ok is True, report
    g = rdflib.Graph().parse(data=ttl, format="turtle")
    ns = rdflib.Namespace("http://example.org/network-ontology#")
    b = ns["binding-intent-1-LatencyMetric"]
    assert (b, ns.forMetric, rdflib.Literal(LAT)) in g
    assert (b, ns.onInterface, rdflib.URIRef(IFACE_IRI)) in g
    assert float(g.value(b, ns.pinnedBound)) == 25.0


def test_render_refuses_metric_without_placement():
    with pytest.raises(BindError):
        render_binding("intent-1", {LAT: 25.0}, {}, {})


def test_refuses_when_bounds_unreadable(tmp_path):
    code, posts = _run(tmp_path, ["--place", "LatencyMetric=olt1/pon0"], intent={"expression": {}})
    assert code != 0
    assert posts == []
    assert not (tmp_path / "abox").exists()


def test_refuses_when_a_metric_has_no_placement(tmp_path):
    code, posts = _run(tmp_path, [])
    assert code != 0
    assert posts == []
    assert not (tmp_path / "abox").exists()


def test_refuses_unknown_interface(tmp_path):
    code, posts = _run(tmp_path, ["--place", "LatencyMetric=olt1/pon9"], iris=())
    assert code != 0
    assert all(u.endswith("/query") for u, _ in posts)
    assert not (tmp_path / "abox").exists()


def test_writes_nothing_on_refusal(tmp_path):
    for args in (["--place", "NoSuchMetric=olt1/pon0"],
                 ["--place", "LatencyMetric=olt1"],
                 ["--place", "LatencyMetric=ol't1/pon0"]):
        code, posts = _run(tmp_path, args)
        assert code != 0
        assert posts == []
        assert not (tmp_path / "abox").exists()


def test_refuses_ambiguous_interface(tmp_path):
    code, _ = _run(tmp_path, ["--place", "LatencyMetric=olt1/pon0"], iris=(IFACE_IRI, IFACE_IRI + "b"))
    assert code != 0
    assert not (tmp_path / "abox").exists()


def test_refuses_metric_missing_from_metric_map(tmp_path):
    with patch.object(bind_intent, "load_metric_map", return_value={}):
        code, posts = _run(tmp_path, ["--place", "LatencyMetric=olt1/pon0"])
    assert code != 0
    assert not (tmp_path / "abox").exists()


def test_server_error_text_is_not_printed(tmp_path, capsys):
    code, _ = _run(tmp_path, ["--place", "LatencyMetric=olt1/pon0"],
                   client_exc=Tmf921Error("SECRET-SERVER-TEXT"))
    assert code != 0
    out = capsys.readouterr()
    assert "SECRET-SERVER-TEXT" not in out.out + out.err


def test_default_writes_file_and_posts(tmp_path):
    code, posts = _run(tmp_path, ["--place", "LatencyMetric=olt1/pon0"])
    assert code == 0
    f = tmp_path / "abox" / "intent-1.ttl"
    assert f.exists()
    data_posts = [p for p in posts if not p[0].endswith("/query")]
    assert len(data_posts) == 1
    assert b"binding-intent-1-LatencyMetric" in data_posts[0][1]["data"]


def test_write_only_does_not_post(tmp_path):
    code, posts = _run(tmp_path, ["--place", "LatencyMetric=olt1/pon0", "--write-only"])
    assert code == 0
    assert (tmp_path / "abox" / "intent-1.ttl").exists()
    assert all(u.endswith("/query") for u, _ in posts)


def test_bad_intent_id_refused(tmp_path):
    with patch("requests.post") as post:
        code = main(["bad id!", "--abox-dir", str(tmp_path / "abox")])
    assert code != 0
    post.assert_not_called()


def test_load_failure_is_reported_as_written_not_loaded(tmp_path, capsys):
    import requests as rq
    client = Mock()
    client.get_intent.return_value = INTENT

    def fake_post(url, **kw):
        if url.endswith("/query"):
            return _sparql_reply([IFACE_IRI])
        raise rq.ConnectionError("boom-secret")

    with patch.object(bind_intent, "Tmf921Client", return_value=client), \
            patch("requests.post", side_effect=fake_post):
        code = main(["intent-1", "--abox-dir", str(tmp_path / "abox"),
                     "--place", "LatencyMetric=olt1/pon0"])
    err = capsys.readouterr().err
    assert code == 1
    assert "did not accept" in err and "boom-secret" not in err
