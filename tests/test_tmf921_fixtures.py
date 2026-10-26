"""Guards the trimmed TMF921 server fixtures: valid JSON, expected keys, no licensed ontology definitions."""
import json
import re
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tmf921"

REQUIRED = {
    "intent.json",
    "report_degraded_no_observation.json",
    "report_fulfilled.json",
    "report_degraded_violation.json",
    "report_degraded_unit_mismatch.json",
    "observation_request.json",
}

_TERMS = (
    "owl:Class|owl:ObjectProperty|owl:DatatypeProperty|owl:Ontology|"
    "rdfs:subClassOf|rdfs:comment|rdfs:isDefinedBy|rdfs:domain|rdfs:range|"
    "skos:definition|dct:description|dcterms:description"
)
_IRIS = (
    r"www\.w3\.org/2002/07/owl#(?:Class|ObjectProperty|DatatypeProperty|Ontology)|"
    r"www\.w3\.org/2000/01/rdf-schema#(?:subClassOf|comment|isDefinedBy|domain|range)|"
    r"www\.w3\.org/2004/02/skos/core#definition|"
    r"purl\.org/dc/(?:terms|elements/1\.1)/description"
)
FORBIDDEN = re.compile(rf"(?:{_TERMS})\b|(?:{_IRIS})")

INTENT_KEYS = {"id", "href", "@type", "name", "description", "lifecycleStatus", "creationDate", "expression"}
REPORT_KEYS = {
    "id", "href", "@type", "@baseType", "@schemaLocation", "name", "creationDate",
    "intentId", "intentHandlingState", "intentHandlingReason", "expression",
}
OBSERVATION_KEYS = {"metricUri", "value", "unit", "obtainedAt"}


def _expected_keys(name):
    if name == "intent.json":
        return INTENT_KEYS
    if name == "observation_request.json":
        return OBSERVATION_KEYS
    return REPORT_KEYS


def test_required_fixtures_exist():
    present = {p.name for p in FIXTURE_DIR.glob("*.json")}
    assert REQUIRED <= present, f"missing: {REQUIRED - present}"


def test_fixtures_are_valid_json_and_free_of_ontology_definitions():
    for name in sorted(REQUIRED):
        text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
        data = json.loads(text)
        assert set(data) <= _expected_keys(name), f"{name} has unexpected keys: {set(data) - _expected_keys(name)}"
        assert not FORBIDDEN.search(text), f"{name} contains an ontology definition"


def test_guard_pattern_catches_definitions():
    for bad in ("rdfs:comment", "skos:definition", "owl:Ontology", "rdfs:domain",
                "<http://purl.org/dc/terms/description>", "http://www.w3.org/2000/01/rdf-schema#range"):
        assert FORBIDDEN.search(bad), bad
