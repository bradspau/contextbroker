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
