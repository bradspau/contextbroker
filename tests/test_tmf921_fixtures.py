"""Guards the trimmed TMF921 server fixtures: valid JSON, no licensed ontology definitions."""
import json
import re
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tmf921"
FORBIDDEN = re.compile(r"owl:Class|rdfs:subClassOf|dct:description")


def test_fixtures_are_valid_json_and_free_of_ontology_definitions():
    files = sorted(FIXTURE_DIR.glob("*.json"))
    assert len(files) >= 6
    for path in files:
        text = path.read_text()
        json.loads(text)
        assert not FORBIDDEN.search(text), f"{path.name} contains an ontology definition"
