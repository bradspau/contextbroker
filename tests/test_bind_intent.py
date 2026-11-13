"""bind_intent against an in-memory stand-in for Fuseki (tests.fluent_helpers.GraphFuseki).

Only requests.post and the TMF921 client are replaced. The stand-in proves what the update
does when it succeeds; it cannot prove Fuseki's transaction, which the live parity test does.
"""
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import rdflib
import requests
from rdflib.plugins.sparql.processor import prepareUpdate

from data_contracts.fluents import fluent_iri
from ingestion import bind_intent
from ingestion.bind_intent import BindError, bind, main, plan_changes
from ingestion.load_ontologies import ABOX_MARKER, validate_shapes
from intent_management.tmf921_client import Tmf921Error
from tests.fluent_helpers import IFACES_TTL, JIT, LAT, NS, T0, T1, T2, GraphFuseki

ROOT = Path(__file__).resolve().parent.parent
SHAPES = ROOT / "ontologies" / "shacl" / "topology.shacl.ttl"
IFACE_IRI = f"{NS}if-olt1-pon0"
IFACE1_IRI = f"{NS}if-olt1-pon1"
BIND = f"{NS}binding-intent-1-LatencyMetric"
PLACE = ["--place", "LatencyMetric=olt1/pon0"]
HOUR = timedelta(hours=1)


def intent_doc(goals):
    """A TMF921 intent whose expression carries one condition per metric."""
    lines = ["@prefix quan: <http://tio.models.tmforum.org/tio/v3.6.0/QuantityOntology/> .",
             "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
             "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> ."]
    for i, (uri, value) in enumerate(goals.items()):
        lines.append(f'_:c{i} quan:smaller ( <{uri}> [ rdf:value "{value}"^^xsd:decimal ] ) .')
    return {"expression": {"expressionValue": "\n".join(lines) + "\n"}}


INTENT = intent_doc({LAT: 25})


@pytest.fixture(autouse=True)
def _fuseki_credential(monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "testpassword0")


def fuseki(ttl=IFACES_TTL) -> GraphFuseki:
    fx = GraphFuseki()
    fx.graph.parse(data=ttl, format="turtle")
    return fx


def _client(intent=INTENT, exc=None):
    client = Mock()
    if exc:
        client.get_intent.side_effect = exc
    else:
        client.get_intent.return_value = intent
    return client


def _run(tmp_path, args, *, fx=None, intent=INTENT, client_exc=None, now=None, post=None):
    """Run main() with the TMF921 client mocked and requests.post served by `fx`."""
    fx = fx if fx is not None else fuseki()
    patches = [patch.object(bind_intent, "Tmf921Client", return_value=_client(intent, client_exc)),
               patch("requests.post", side_effect=post or fx.post)]
    if now is not None:
        patches.append(patch.object(bind_intent, "_utcnow", return_value=now))
    for p in patches:
        p.start()
    try:
        code = main(["intent-1", "--abox-dir", str(tmp_path / "abox"), *args])
    finally:
        for p in reversed(patches):
            p.stop()
    return code, fx


def _writes(fx):
    return [p for p in fx.posts if not p[0].endswith("/query")]


def _conforms(graph, tmp_path):
    f = tmp_path / "whole.ttl"
    f.write_text(graph.serialize(format="turtle"), encoding="utf-8")
    ok, report = validate_shapes(f, SHAPES)
    assert ok is True, report


def _first_bind(tmp_path, fx=None, **kw):
    code, fx = _run(tmp_path, PLACE, fx=fx, now=T0, **kw)
    assert code == 0
    return fx


# ---- plan_changes ---------------------------------------------------------------------


def _plan(bounds=None, **kw):
    bounds = bounds if bounds is not None else {LAT: 25.0}
    return plan_changes("intent-1", bounds, {u: ("olt1", "pon0") for u in bounds},
                        {("olt1", "pon0"): IFACE_IRI}, kw.pop("stored", {}), kw.pop("now", T0), **kw)


def test_plan_changes_first_bind_roundtrips_through_the_shacl_shape(tmp_path):
    g, olds, _ = _plan()
    assert olds == []
    _conforms(g, tmp_path)
    b = rdflib.URIRef(BIND)
    assert (b, rdflib.URIRef(NS + "forMetric"), rdflib.Literal(LAT)) in g
    goal = g.value(b, rdflib.URIRef(NS + "hasGoal"))
    place = g.value(b, rdflib.URIRef(NS + "hasPlacement"))
    assert float(g.value(goal, rdflib.URIRef(NS + "pinnedBound"))) == 25.0
    assert g.value(place, rdflib.URIRef(NS + "onInterface")) == rdflib.URIRef(IFACE_IRI)
    # the retired shape is not written
    assert g.value(b, rdflib.URIRef(NS + "pinnedBound")) is None
    assert g.value(b, rdflib.URIRef(NS + "onInterface")) is None


def test_plan_changes_refuses_metric_without_placement():
    with pytest.raises(BindError):
        plan_changes("intent-1", {LAT: 25.0}, {}, {}, {}, T0)


def test_plan_changes_refuses_no_goals():
    with pytest.raises(BindError, match="no readable goals"):
        plan_changes("intent-1", {}, {}, {}, {}, T0)


def test_plan_changes_unusable_uri_raises_binderror():
    with pytest.raises(BindError):
        plan_changes("intent-1", {"not a uri": 1.0}, {"not a uri": ("a", "b")},
                     {("a", "b"): IFACE_IRI}, {}, T0)


@pytest.mark.parametrize("value", [0.1, 99.95])
def test_plan_changes_roundtrips_non_integer_goals(value):
    g, _, _ = _plan({LAT: value})
    goal = g.value(rdflib.URIRef(BIND), rdflib.URIRef(NS + "hasGoal"))
    assert g.value(goal, rdflib.URIRef(NS + "pinnedBound")).toPython() == value


# ---- the first bind -------------------------------------------------------------------------


def test_first_bind_is_one_update_post_and_then_the_file(tmp_path):
    code, fx = _run(tmp_path, PLACE, now=T0)
    assert code == 0
    updates = [p for p in fx.posts if p[0].endswith("/update")]
    assert len(updates) == 1
    assert not any(p[0].endswith("/data") for p in fx.posts)
    assert updates[0][1]["headers"]["Content-Type"] == "application/sparql-update"
    assert updates[0][1]["auth"] == ("admin", "testpassword0")
    assert len(prepareUpdate(fx.updates()[0]).algebra) == 1

    goal = rdflib.URIRef(fluent_iri(BIND, "goal", T0))
    place = rdflib.URIRef(fluent_iri(BIND, "place", T0))
    assert (rdflib.URIRef(BIND), rdflib.RDF.type, rdflib.URIRef(NS + "ServiceBinding")) in fx.graph
    assert float(fx.graph.value(goal, rdflib.URIRef(NS + "pinnedBound"))) == 25.0
    for node in (goal, place):
        assert fx.graph.value(node, rdflib.URIRef(NS + "validStart")) == \
            fx.graph.value(node, rdflib.URIRef(NS + "recordedAt"))
        assert fx.graph.value(node, rdflib.URIRef(NS + "validEnd")) is None
    _conforms(fx.graph, tmp_path)

    text = (tmp_path / "abox" / "intent-1.ttl").read_text(encoding="utf-8")
    assert text.splitlines()[0] == ABOX_MARKER
    parsed = rdflib.Graph().parse(data=text, format="turtle")
    assert (goal, rdflib.RDF.type, rdflib.URIRef(NS + "GoalFluent")) in parsed
    _conforms(parsed, tmp_path)


def test_write_only_writes_the_marked_file_and_posts_nothing(tmp_path):
    code, fx = _run(tmp_path, [*PLACE, "--write-only"])
    assert code == 0
    assert (tmp_path / "abox" / "intent-1.ttl").read_text().splitlines()[0] == ABOX_MARKER
    assert all(u.endswith("/query") for u, _ in fx.posts)


def test_write_only_replace_never_applies(tmp_path):
    fx = _first_bind(tmp_path)
    before = set(fx.graph)
    code, fx = _run(tmp_path, [*PLACE, "--replace", "--write-only"], fx=fx, now=T1,
                    intent=intent_doc({LAT: 30}))
    assert code == 0
    assert set(fx.graph) == before
    assert len(fx.updates()) == 1  # only the first bind's


# ---- refusals before any write ------------------------------------------------------------


def test_refuses_when_goals_unreadable(tmp_path):
    code, fx = _run(tmp_path, PLACE, intent={"expression": {}})
    assert code != 0
    assert _writes(fx) == []
    assert not (tmp_path / "abox").exists()


def test_refuses_when_a_metric_has_no_placement(tmp_path):
    code, fx = _run(tmp_path, [])
    assert code != 0
    assert _writes(fx) == []
    assert not (tmp_path / "abox").exists()


def test_refuses_unknown_interface(tmp_path):
    code, fx = _run(tmp_path, ["--place", "LatencyMetric=olt1/pon9"])
    assert code != 0
    assert all(u.endswith("/query") for u, _ in fx.posts)
    assert not (tmp_path / "abox").exists()


def test_writes_nothing_on_refusal(tmp_path):
    for args in (["--place", "NoSuchMetric=olt1/pon0"],
                 ["--place", "LatencyMetric=olt1"],
                 ["--place", "LatencyMetric=ol't1/pon0"]):
        code, fx = _run(tmp_path, args)
        assert code != 0
        assert _writes(fx) == []
        assert not (tmp_path / "abox").exists()


def test_refuses_ambiguous_interface(tmp_path):
    twin = IFACES_TTL + f'\n<{NS}if-twin> a :Interface ; :onDevice "olt1" ; :interfaceName "pon0" .'
    code, fx = _run(tmp_path, PLACE, fx=fuseki(f"@prefix : <{NS}> .\n{twin}"))
    assert code != 0
    assert _writes(fx) == []
    assert not (tmp_path / "abox").exists()


def test_refuses_metric_missing_from_metric_map(tmp_path):
    with patch.object(bind_intent, "load_metric_map", return_value={}):
        code, fx = _run(tmp_path, PLACE)
    assert code != 0
    assert _writes(fx) == []
    assert not (tmp_path / "abox").exists()


def test_server_error_text_is_not_printed(tmp_path, capsys):
    code, _ = _run(tmp_path, PLACE, client_exc=Tmf921Error("SECRET-SERVER-TEXT"))
    assert code != 0
    out = capsys.readouterr()
    assert "SECRET-SERVER-TEXT" not in out.out + out.err


def test_bad_intent_id_refused(tmp_path):
    with patch("requests.post") as post:
        code = main(["bad id!", "--abox-dir", str(tmp_path / "abox")])
    assert code != 0
    post.assert_not_called()


def test_malformed_sparql_json_is_refused(tmp_path, capsys):
    fx = fuseki()

    def post(url, **kw):
        if url.endswith("/query"):
            r = Mock()
            r.raise_for_status = Mock()
            r.json.return_value = {"nope": 1}
            return r
        return fx.post(url, **kw)

    code, _ = _run(tmp_path, PLACE, fx=fx, post=post)
    assert code != 0
    assert not (tmp_path / "abox").exists()
    assert "Refused" in capsys.readouterr().err


def test_metric_map_error_is_refused(tmp_path, capsys):
    with patch.object(bind_intent, "load_metric_map", side_effect=ValueError("secret")):
        code, _ = _run(tmp_path, PLACE)
    err = capsys.readouterr().err
    assert code != 0 and "Refused" in err and "secret" not in err
    assert not (tmp_path / "abox").exists()


def test_shacl_parse_error_is_refused(tmp_path, capsys):
    with patch.object(bind_intent, "validate_shapes", side_effect=Exception("parse")):
        code, fx = _run(tmp_path, PLACE)
    assert code != 0 and "Refused" in capsys.readouterr().err
    assert _writes(fx) == []
    assert not (tmp_path / "abox").exists()


def test_write_oserror_before_the_update_leaves_the_graph_unchanged(tmp_path, capsys):
    (tmp_path / "abox").write_text("i am a file, not a directory")
    code, fx = _run(tmp_path, PLACE)
    assert code != 0
    assert _writes(fx) == []
    assert "Refused" in capsys.readouterr().err


# ---- an existing binding ---------------------------------------------------------------------


def test_existing_binding_without_a_flag_names_both_flags(tmp_path, capsys):
    fx = _first_bind(tmp_path)
    n = len(fx.updates())
    code, fx = _run(tmp_path, PLACE, fx=fx, now=T1)
    err = capsys.readouterr().err
    assert code == 1
    assert "--replace" in err and "--correct" in err
    assert len(fx.updates()) == n


def test_replace_with_nothing_changed_writes_nothing(tmp_path, capsys):
    fx = _first_bind(tmp_path)
    f = tmp_path / "abox" / "intent-1.ttl"
    before = (f.read_bytes(), f.stat().st_mtime_ns, set(fx.graph))
    n = len(fx.updates())
    capsys.readouterr()
    for flag in ("--replace", "--correct"):
        code, fx = _run(tmp_path, [*PLACE, flag], fx=fx, now=T1)
        assert code == 0
        assert "Nothing changed" in capsys.readouterr().out
    assert len(fx.updates()) == n
    assert (f.read_bytes(), f.stat().st_mtime_ns, set(fx.graph)) == before


def test_replace_changed_goal_is_one_update_and_keeps_the_old_goal(tmp_path):
    fx = _first_bind(tmp_path)
    n = len(fx.updates())
    code, fx = _run(tmp_path, [*PLACE, "--replace"], fx=fx, now=T1, intent=intent_doc({LAT: 30}))
    assert code == 0
    assert len(fx.updates()) == n + 1
    assert not any(p[0].endswith("/data") for p in fx.posts)
    old = rdflib.URIRef(fluent_iri(BIND, "goal", T0))
    new = rdflib.URIRef(fluent_iri(BIND, "goal", T1))
    assert float(fx.graph.value(old, rdflib.URIRef(NS + "pinnedBound"))) == 25.0
    assert fx.graph.value(old, rdflib.URIRef(NS + "validEnd")) is not None
    assert float(fx.graph.value(new, rdflib.URIRef(NS + "pinnedBound"))) == 30.0
    assert (rdflib.URIRef(fluent_iri(BIND, "place", T1)), None, None) not in fx.graph
    _conforms(fx.graph, tmp_path)


# ---- failure of the one update ---------------------------------------------------------------


def test_update_failure_changes_nothing_and_writes_no_file(tmp_path, capsys):
    fx = fuseki()
    before = set(fx.graph)
    attempts = []

    def post(url, **kw):
        if url.endswith("/update"):
            attempts.append(url)
            raise requests.ConnectionError("boom-secret")
        return fx.post(url, **kw)

    code, _ = _run(tmp_path, PLACE, fx=fx, post=post, now=T0)
    err = capsys.readouterr().err
    assert code == 1
    assert "did not confirm" in err and "nothing changed" in err and "boom-secret" not in err
    assert len(attempts) == 1
    assert set(fx.graph) == before
    assert not (tmp_path / "abox" / "intent-1.ttl").exists()


def test_update_failure_leaves_an_earlier_file_as_it_was(tmp_path):
    fx = _first_bind(tmp_path)
    f = tmp_path / "abox" / "intent-1.ttl"
    before = f.read_bytes()

    def post(url, **kw):
        if url.endswith("/update"):
            raise requests.ConnectionError("x")
        return fx.post(url, **kw)

    code, _ = _run(tmp_path, [*PLACE, "--replace"], fx=fx, post=post, now=T1,
                   intent=intent_doc({LAT: 30}))
    assert code == 1
    assert f.read_bytes() == before


def test_a_blocked_update_is_reported_and_no_file_is_written(tmp_path, capsys):
    fx = fuseki()

    def post(url, **kw):
        if url.endswith("/update"):
            fx.posts.append((url, kw))
            return Mock(raise_for_status=Mock(return_value=None))  # accepted, applied nothing
        return fx.post(url, **kw)

    code, _ = _run(tmp_path, PLACE, fx=fx, post=post, now=T0)
    err = capsys.readouterr().err
    assert code == 1
    assert "the binding changed while it was being written; nothing was changed" in err
    assert not (tmp_path / "abox").exists() or not (tmp_path / "abox" / "intent-1.ttl").exists()


def test_a_file_that_cannot_be_written_after_the_update_says_so(tmp_path, capsys):
    (tmp_path / "abox" / "intent-1.ttl").mkdir(parents=True)  # a directory where the file goes
    code, fx = _run(tmp_path, PLACE, now=T0)
    err = capsys.readouterr().err
    assert code == 1
    assert "applied to the network graph" in err and "file could not be written" in err
    assert len(fx.updates()) == 1


def test_an_unwritable_abox_directory_is_refused_before_the_update(tmp_path, capsys):
    abox = tmp_path / "abox"
    abox.mkdir()
    abox.chmod(0o555)
    try:
        if os.access(abox, os.W_OK):
            pytest.skip("directory permissions are not enforced for this user")
        code, fx = _run(tmp_path, PLACE, now=T0)
    finally:
        abox.chmod(0o755)
    err = capsys.readouterr().err
    assert code == 1
    assert "not writable" in err and "Nothing was written or loaded" in err
    assert fx.updates() == []


# ---- the credential comes only from the environment ---------------------------------------


def test_main_exits_naming_the_variable_before_any_network_call(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
    for extra in ([], ["--write-only"], ["--replace"], ["--correct"]):
        with patch.object(bind_intent, "Tmf921Client") as client_cls, \
                patch("requests.post") as post, patch("requests.get") as get:
            code = main(["intent-1", "--abox-dir", str(tmp_path / "abox"), *PLACE, *extra])
        assert code == 1
        err = capsys.readouterr().err
        assert "FUSEKI_ADMIN_PASSWORD" in err
        post.assert_not_called()
        get.assert_not_called()
        client_cls.assert_not_called()
    assert not (tmp_path / "abox").exists()


def test_queries_and_updates_carry_the_credential_when_set(tmp_path):
    code, fx = _run(tmp_path, PLACE, now=T0)
    assert code == 0
    queries = [kw for u, kw in fx.posts if u.endswith("/query")]
    assert len(queries) >= 3  # the stored binding, the interface, the confirmation
    for _, kw in fx.posts:
        assert kw["auth"] == ("admin", "testpassword0")


def test_the_update_uses_the_environment_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "other0value")
    code, fx = _run(tmp_path, PLACE, now=T0)
    assert code == 0
    assert all(kw["auth"] == ("admin", "other0value") for _, kw in fx.posts)


def test_credential_value_never_reaches_output(tmp_path, capsys):
    _run(tmp_path, PLACE)
    out = capsys.readouterr()
    assert "testpassword0" not in out.out + out.err


# ---- --correct, --valid-from and the flag rules --------------------------------------------


def _stored(tmp_path):
    return _first_bind(tmp_path)


def _refused(tmp_path, args, *, fx, intent=INTENT, now=T1):
    n = len(fx.updates())
    before = set(fx.graph)
    code, fx = _run(tmp_path, args, fx=fx, now=now, intent=intent)
    assert code == 1
    assert len(fx.updates()) == n
    assert set(fx.graph) == before


def test_replace_and_correct_together_are_refused(tmp_path):
    fx = _stored(tmp_path)
    _refused(tmp_path, [*PLACE, "--replace", "--correct"], fx=fx)


def test_valid_from_needs_replace(tmp_path, capsys):
    fx = _stored(tmp_path)
    _refused(tmp_path, [*PLACE, "--valid-from", "2026-11-02T10:00:00Z"], fx=fx)
    _refused(tmp_path, [*PLACE, "--correct", "--valid-from", "2026-11-02T10:00:00Z"], fx=fx)


@pytest.mark.parametrize("text", ["2026-11-05", "2026-11-05T10:00:00",
                                  "2026-11-05T10:00:00+01:00", "yesterday",
                                  "2026-11-05T10:00:00.1234567Z", "2026-13-05T10:00:00Z",
                                  "' ; DROP ALL ; '"])
def test_a_malformed_valid_from_is_refused_without_echo(tmp_path, capsys, text):
    fx = _stored(tmp_path)
    capsys.readouterr()
    _refused(tmp_path, [*PLACE, "--replace", "--valid-from", text], fx=fx,
             intent=intent_doc({LAT: 30}))
    err = capsys.readouterr().err
    assert "Refused" in err and text not in err


def test_valid_from_later_than_now_is_refused(tmp_path):
    fx = _stored(tmp_path)
    _refused(tmp_path, [*PLACE, "--replace", "--valid-from", "2026-11-03T10:00:01Z"], fx=fx,
             intent=intent_doc({LAT: 30}), now=T1)


def test_valid_from_not_later_than_the_current_goal_start_is_refused(tmp_path):
    fx = _stored(tmp_path)
    _refused(tmp_path, [*PLACE, "--replace", "--valid-from", "2026-11-01T10:00:00Z"], fx=fx,
             intent=intent_doc({LAT: 30}))


def test_correct_with_no_stored_binding_is_refused(tmp_path):
    fx = fuseki()
    code, fx = _run(tmp_path, [*PLACE, "--correct"], fx=fx, now=T1)
    assert code == 1
    assert fx.updates() == []


def test_d17_replace_and_correct_refuse_to_drop_a_metric(tmp_path, capsys):
    code, fx = _run(tmp_path, [*PLACE, "--place", "JitterMetric=olt1/pon1"], now=T0,
                    intent=intent_doc({LAT: 25, JIT: 5}))
    assert code == 0
    capsys.readouterr()
    for flag in ("--replace", "--correct"):
        _refused(tmp_path, [*PLACE, flag], fx=fx, intent=intent_doc({LAT: 25}))
        assert "the intent no longer has JitterMetric; bind it under a new intent id" in \
            capsys.readouterr().err


def test_a_metric_new_to_the_intent_is_created_under_replace(tmp_path):
    fx = _stored(tmp_path)
    code, fx = _run(tmp_path, [*PLACE, "--place", "JitterMetric=olt1/pon1", "--replace"], fx=fx,
                    now=T1, intent=intent_doc({LAT: 25, JIT: 5}))
    assert code == 0
    jit = f"{NS}binding-intent-1-JitterMetric"
    assert (rdflib.URIRef(jit), rdflib.RDF.type, rdflib.URIRef(NS + "ServiceBinding")) in fx.graph
    assert (rdflib.URIRef(fluent_iri(jit, "goal", T1)), rdflib.RDF.type,
            rdflib.URIRef(NS + "GoalFluent")) in fx.graph
    # the unchanged metric got no new fluent
    assert (rdflib.URIRef(fluent_iri(BIND, "goal", T1)), None, None) not in fx.graph


def test_a_stored_metric_with_two_open_goals_is_not_usable(tmp_path, capsys):
    fx = _stored(tmp_path)
    extra = rdflib.Graph()
    extra.parse(data=f"""@prefix : <{NS}> .
<{fluent_iri(BIND, 'goal', T1)}> a :GoalFluent ; :pinnedBound "9.0"^^<http://www.w3.org/2001/XMLSchema#double> ;
  :validStart "2026-11-03T10:00:00.000Z"^^<http://www.w3.org/2001/XMLSchema#dateTime> ;
  :recordedAt "2026-11-03T10:00:00.000Z"^^<http://www.w3.org/2001/XMLSchema#dateTime> .
<{BIND}> :hasGoal <{fluent_iri(BIND, 'goal', T1)}> .""", format="turtle")
    for t in extra:
        fx.graph.add(t)
    capsys.readouterr()
    _refused(tmp_path, [*PLACE, "--replace"], fx=fx, intent=intent_doc({LAT: 30}), now=T2)
    assert "the stored binding is not usable; nothing was changed" in capsys.readouterr().err


def test_the_help_lists_the_new_flags(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "--correct" in out and "--valid-from" in out


def test_bind_returns_none_when_nothing_changed(tmp_path):
    fx = _first_bind(tmp_path)
    with patch("requests.post", side_effect=fx.post):
        out, applied = bind("intent-1", ["LatencyMetric=olt1/pon0"], abox_dir=tmp_path / "a2",
                            client=_client(), replace=True, now=T1)
    assert (out, applied) == (None, False)
    assert not (tmp_path / "a2").exists()
