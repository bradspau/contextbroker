from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import ingestion.load_ontologies as loader
from ingestion.load_ontologies import load_ontologies, validate_shapes

ROOT = Path(__file__).resolve().parent.parent
TBOX_DIR = ROOT / "ontologies" / "tbox"
SHACL_DIR = ROOT / "ontologies" / "shacl"
SHAPES = SHACL_DIR / "topology.shacl.ttl"
ENDPOINT = "http://localhost:3030/network-dataset/data"

def _write(tmp_path, text):
    d = tmp_path / "tbox"
    d.mkdir()
    (d / "bad.ttl").write_text(text)
    return d


def test_load_ontologies_posts_nothing_on_violation(tmp_path):
    d = _write(tmp_path, _binding(forIntent=False))
    with patch("requests.post") as post:
        with pytest.raises(ValueError, match="forIntent"):
            load_ontologies(d, SHACL_DIR, ENDPOINT, allow=("bad.ttl",))
    post.assert_not_called()


def test_repo_files_validate_and_load_one_post_each():
    ok, report = validate_shapes(TBOX_DIR / "topology.ttl", SHAPES)
    assert ok, report
    resp = Mock()
    resp.raise_for_status = Mock()
    with patch("requests.post", return_value=resp) as post:
        load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT)
    assert post.call_count == len(list(TBOX_DIR.glob("*.ttl")))


def test_empty_tbox_dir_raises(tmp_path):
    shacl = tmp_path / "shacl"
    shacl.mkdir()
    (shacl / "s.ttl").write_text("")
    tbox = tmp_path / "tbox"
    tbox.mkdir()
    with patch("ingestion.load_ontologies.requests.post") as post:
        with pytest.raises(ValueError, match="tbox_dir"):
            load_ontologies(tbox, shacl, "http://x/data")
    post.assert_not_called()


def test_empty_shacl_dir_raises(tmp_path):
    tbox = tmp_path / "tbox"
    tbox.mkdir()
    (tbox / "t.ttl").write_text("")
    shacl = tmp_path / "shacl"
    shacl.mkdir()
    with patch("ingestion.load_ontologies.requests.post") as post:
        with pytest.raises(ValueError, match="shacl_dir"):
            load_ontologies(tbox, shacl, "http://x/data", allow=("t.ttl",))
    post.assert_not_called()


BINDING_PREFIXES = """@prefix : <http://example.org/network-ontology#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""


def _binding(forIntent=True, forMetric=True, iface=True, pins=('"0.0"^^xsd:double',)):
    parts = []
    if forIntent:
        parts.append(':forIntent "intent-1"')
    if forMetric:
        parts.append(':forMetric "http://example.org/metric/input_drops"')
    if iface:
        parts.append(":onInterface :if-router-a-ge000")
    for p in pins:
        parts.append(f":pinnedBound {p}")
    return BINDING_PREFIXES + ":b1 a :ServiceBinding ; " + " ; ".join(parts) + " .\n"


def _abox_file(tmp_path, text, name="b.ttl"):
    d = tmp_path / "abox"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_text(text)
    return f


def test_service_binding_shape_accepts_complete_binding(tmp_path):
    ok, report = validate_shapes(_abox_file(tmp_path, _binding()), SHAPES)
    assert ok is True, report


def test_service_binding_shape_rejects_missing_interface(tmp_path):
    ok, report = validate_shapes(_abox_file(tmp_path, _binding(iface=False)), SHAPES)
    assert ok is False
    assert "onInterface" in report


def test_service_binding_shape_rejects_two_pins(tmp_path):
    text = _binding(pins=('"0.0"^^xsd:double', '"1.0"^^xsd:double'))
    ok, report = validate_shapes(_abox_file(tmp_path, text), SHAPES)
    assert ok is False
    assert "pinnedBound" in report


def test_non_numeric_pin_is_rejected(tmp_path):
    ok, report = validate_shapes(_abox_file(tmp_path, _binding(pins=('"abc"',))), SHAPES)
    assert ok is False
    assert "pinnedBound" in report


def test_integer_pin_is_rejected(tmp_path):
    ok, report = validate_shapes(_abox_file(tmp_path, _binding(pins=("5",))), SHAPES)
    assert ok is False
    assert "pinnedBound" in report


def test_service_binding_shape_rejects_missing_intent(tmp_path):
    ok, report = validate_shapes(_abox_file(tmp_path, _binding(forIntent=False)), SHAPES)
    assert ok is False
    assert "forIntent" in report


def test_service_binding_shape_rejects_missing_metric(tmp_path):
    ok, report = validate_shapes(_abox_file(tmp_path, _binding(forMetric=False)), SHAPES)
    assert ok is False
    assert "forMetric" in report


def test_abox_failure_loads_nothing(tmp_path):
    _abox_file(tmp_path, _binding(iface=False))
    with patch("requests.post") as post:
        with pytest.raises(ValueError, match="onInterface"):
            load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=tmp_path / "abox")
    post.assert_not_called()


def test_abox_dir_absent_is_not_an_error(tmp_path):
    resp = Mock()
    resp.raise_for_status = Mock()
    with patch("requests.post", return_value=resp) as post:
        load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=tmp_path / "nope")
    assert post.call_count == len(list(TBOX_DIR.glob("*.ttl")))


def test_abox_files_are_posted_after_validation(tmp_path):
    _abox_file(tmp_path, _binding())
    resp = Mock()
    resp.raise_for_status = Mock()
    with patch("requests.post", return_value=resp) as post:
        load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=tmp_path / "abox")
    assert post.call_count == len(list(TBOX_DIR.glob("*.ttl"))) + 1


def test_empty_abox_dir_is_fine(tmp_path):
    (tmp_path / "abox").mkdir()
    resp = Mock()
    resp.raise_for_status = Mock()
    with patch("requests.post", return_value=resp) as post:
        load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=tmp_path / "abox")
    assert post.call_count == len(list(TBOX_DIR.glob("*.ttl")))


def _tbox_with(tmp_path, names):
    d = tmp_path / "tbox"
    d.mkdir()
    for n in names:
        (d / n).write_text((TBOX_DIR / "topology.ttl").read_text())
    return d


def test_unlisted_tbox_file_is_refused_before_validation_and_post(tmp_path):
    d = _tbox_with(tmp_path, ["topology.ttl", "licensed.ttl"])
    with patch("ingestion.load_ontologies.validate_shapes") as validate, \
            patch("ingestion.load_ontologies.requests.post") as post:
        with pytest.raises(ValueError, match="not on the allow-list; nothing loaded") as exc:
            load_ontologies(d, SHACL_DIR, ENDPOINT)
    assert "licensed.ttl" in str(exc.value)
    assert "topology.ttl" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    validate.assert_not_called()
    post.assert_not_called()


def test_symlink_outside_tbox_dir_is_refused(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "topology.ttl").write_text((TBOX_DIR / "topology.ttl").read_text())
    d = tmp_path / "tbox"
    d.mkdir()
    try:
        (d / "topology.ttl").symlink_to(outside / "topology.ttl")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not supported on this platform")
    with patch("ingestion.load_ontologies.validate_shapes") as validate, \
            patch("ingestion.load_ontologies.requests.post") as post:
        with pytest.raises(ValueError, match="links outside the TBox directory; nothing loaded") as exc:
            load_ontologies(d, SHACL_DIR, ENDPOINT)
    assert "topology.ttl" in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    validate.assert_not_called()
    post.assert_not_called()


def test_repo_tbox_passes_the_allow_list():
    assert getattr(loader, "TBOX_ALLOWLIST", None) == ("topology.ttl",)
    assert {p.name for p in TBOX_DIR.glob("*.ttl")} <= set(loader.TBOX_ALLOWLIST)
    resp = Mock()
    resp.raise_for_status = Mock()
    with patch("requests.post", return_value=resp) as post:
        load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT)
    assert post.call_count == len(list(TBOX_DIR.glob("*.ttl")))


def test_abox_files_are_not_subject_to_the_tbox_allow_list(tmp_path):
    _abox_file(tmp_path, _binding(), name="not-on-the-list.ttl")
    resp = Mock()
    resp.raise_for_status = Mock()
    with patch("requests.post", return_value=resp) as post:
        load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=tmp_path / "abox")
    assert post.call_count == len(list(TBOX_DIR.glob("*.ttl"))) + 1


def test_caller_can_pass_its_own_allow_list(tmp_path):
    d = _tbox_with(tmp_path, ["mine.ttl"])
    resp = Mock()
    resp.raise_for_status = Mock()
    with patch("requests.post", return_value=resp) as post:
        load_ontologies(d, SHACL_DIR, ENDPOINT, allow=("mine.ttl",))
    assert post.call_count == 1


# --- retiring the graph intent vocabulary (D-09) ---

MIGRATION = ROOT / "ontologies" / "migrations" / "001-retire-operational-intent.ru"

# The block that topology.ttl carried before it was retired (our own text, copied
# before it was deleted). Old TBox = the current TBox plus this block.
OLD_BLOCK = """@prefix : <http://example.org/network-ontology#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

:OperationalIntent a owl:Class ;
    rdfs:label "Operational Intent" .

:hasTargetMetric a owl:DatatypeProperty ;
    rdfs:domain :OperationalIntent ;
    rdfs:range xsd:string .

:hasOperator a owl:DatatypeProperty ;
    rdfs:domain :OperationalIntent ;
    rdfs:range xsd:string .

:hasTargetValue a owl:DatatypeProperty ;
    rdfs:domain :OperationalIntent ;
    rdfs:range xsd:double .

:appliesToLink a owl:ObjectProperty ;
    rdfs:domain :OperationalIntent ;
    rdfs:range :TopologicalLink .

:stateCategory a owl:DatatypeProperty ;
    rdfs:domain :OperationalIntent ;
    rdfs:range xsd:string ;
    rdfs:comment "One of: intended, configured, observed, inferred." .

:validFrom a owl:DatatypeProperty ;
    rdfs:domain :OperationalIntent ;
    rdfs:range xsd:dateTime .

:validTo a owl:DatatypeProperty ;
    rdfs:domain :OperationalIntent ;
    rdfs:range xsd:dateTime .

:intent-core-health-01 a :OperationalIntent ;
    rdfs:label "Core Health: Zero Input Drops" ;
    :hasTargetMetric "input_drops" ;
    :hasOperator "==" ;
    :hasTargetValue "0.0"^^xsd:double ;
    :stateCategory "intended" ;
    :appliesToLink :link-router-a-router-b .
"""

# A second instance, as a store could hold after an operator created one by hand.
EXTRA_INSTANCE = """@prefix : <http://example.org/network-ontology#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
:hand-made a :OperationalIntent ;
    :hasTargetMetric "latency_ms" ; :hasOperator "<" ;
    :hasTargetValue "5.0"^^xsd:double ;
    :validFrom "2026-01-01T00:00:00Z"^^xsd:dateTime ;
    :appliesToLink :link-router-a-router-b .
"""

RETIRED_NAMES = ("OperationalIntent", "OperationalIntentShape", "intent-core-health-01",
                 "hasTargetMetric", "hasOperator", "hasTargetValue", "appliesToLink",
                 "stateCategory", "validFrom", "validTo")
NETWORK_NS = "http://example.org/network-ontology#"


def _graph(*texts):
    import rdflib
    g = rdflib.Graph()
    for t in texts:
        g.parse(data=t, format="turtle")
    return g


def _binding_fixture():
    return _binding()  # a complete :ServiceBinding pointing at an existing interface


def test_tbox_has_no_graph_intent_vocabulary():
    import rdflib
    g = rdflib.Graph()
    g.parse(TBOX_DIR / "topology.ttl", format="turtle")
    g.parse(SHAPES, format="turtle")
    retired = {rdflib.URIRef(NETWORK_NS + n) for n in RETIRED_NAMES}
    hits = [t for t in g if any(x in retired for x in t)]
    assert hits == []
    assert not [t for t in g if any("OperationalIntent" in str(x)
                                    or "intent-core-health-01" in str(x) for x in t)]
    # What stays: bindings and topology.
    for kept in ("ServiceBinding", "forIntent", "forMetric", "onInterface", "pinnedBound",
                 "TopologicalLink", "Interface", "hasInterface", "onDevice", "interfaceName"):
        assert (rdflib.URIRef(NETWORK_NS + kept), None, None) in g


def test_retire_migration_turns_the_old_tbox_into_the_new_one():
    from rdflib.compare import isomorphic
    new_tbox = (TBOX_DIR / "topology.ttl").read_text()
    fixture = _binding_fixture()
    old = _graph(new_tbox, OLD_BLOCK, EXTRA_INSTANCE, fixture)
    assert len(old) > len(_graph(new_tbox, fixture))
    old.update(MIGRATION.read_text())
    assert isomorphic(old, _graph(new_tbox, fixture))


def test_retire_migration_is_a_no_op_on_the_new_tbox():
    from rdflib.compare import isomorphic
    g = _graph((TBOX_DIR / "topology.ttl").read_text(), _binding_fixture())
    before = len(g)
    g.update(MIGRATION.read_text())
    assert len(g) == before
    assert isomorphic(g, _graph((TBOX_DIR / "topology.ttl").read_text(), _binding_fixture()))


def test_retire_migration_only_deletes():
    import re
    text = MIGRATION.read_text()
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert re.search(r"\bDELETE\b", body, re.I)
    for word in ("INSERT", "CLEAR", "DROP", "LOAD", "CREATE"):
        assert not re.search(rf"\b{word}\b", body, re.I), word
