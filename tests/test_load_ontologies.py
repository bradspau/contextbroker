from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from ingestion.load_ontologies import load_ontologies, validate_shapes

ROOT = Path(__file__).resolve().parent.parent
TBOX_DIR = ROOT / "ontologies" / "tbox"
SHACL_DIR = ROOT / "ontologies" / "shacl"
SHAPES = SHACL_DIR / "topology.shacl.ttl"
ENDPOINT = "http://localhost:3030/network-dataset/data"

PREFIXES = """@prefix : <http://example.org/network-ontology#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
:if-a a :Interface ; :onDevice "R-A" ; :interfaceName "ge0" .
:link a :TopologicalLink ; :hasInterface :if-a .
"""


def _intent(extra="", operator=True, link=True):
    body = ':i1 a :OperationalIntent ; :hasTargetMetric "input_drops" ; :hasTargetValue "0"^^xsd:double'
    if operator:
        body += ' ; :hasOperator "=="'
    if link:
        body += " ; :appliesToLink :link"
    return PREFIXES + body + extra + " .\n"


def _write(tmp_path, text):
    d = tmp_path / "tbox"
    d.mkdir()
    (d / "bad.ttl").write_text(text)
    return d


def test_missing_operator_fails_validation(tmp_path):
    f = _write(tmp_path, _intent(operator=False)) / "bad.ttl"
    ok, report = validate_shapes(f, SHAPES)
    assert ok is False
    assert "hasOperator" in report


def test_missing_applies_to_link_fails(tmp_path):
    f = _write(tmp_path, _intent(link=False)) / "bad.ttl"
    assert validate_shapes(f, SHAPES)[0] is False


def test_bogus_state_category_rejected(tmp_path):
    f = _write(tmp_path, _intent(' ; :stateCategory "bogus"')) / "bad.ttl"
    assert validate_shapes(f, SHAPES)[0] is False


def test_observed_state_category_accepted(tmp_path):
    f = _write(tmp_path, _intent(' ; :stateCategory "observed"')) / "bad.ttl"
    ok, report = validate_shapes(f, SHAPES)
    assert ok is True, report


def test_load_ontologies_posts_nothing_on_violation(tmp_path):
    d = _write(tmp_path, _intent(operator=False))
    with patch("requests.post") as post:
        with pytest.raises(ValueError, match="hasOperator"):
            load_ontologies(d, SHACL_DIR, ENDPOINT)
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
            load_ontologies(tbox, shacl, "http://x/data")
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
