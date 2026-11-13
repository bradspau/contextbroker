"""The knowledge loader: a reviewed bundle file becomes a bundle, or is refused with a fixed reason."""
import hashlib
import logging

import pytest

from artefact_governance.action_catalogue import _version_key, version_key
from data_contracts.knowledge import Match, applicable
from okf_retrieval import loader
from okf_retrieval.loader import (
    LOAD_REASONS,
    MAX_BODY_CHARS,
    MAX_FILE_BYTES,
    LoadError,
    load_bundles,
    parse_bundle,
)
from tests.okf_helpers import bundle_text, write_bundle, write_raw

INTERFACE = dict(
    entities=["Router-*"], purposes=["diagnosis", "assurance"], ontology_classes=["Interface"],
    source_references=["repo:design/event-store.md"],
    linked_actions=["example-collect-interface-counters"])


def _write_interface(tmp_path):
    return write_bundle(tmp_path, "interface-error-handling", "1.0", **INTERFACE)


def test_a_bundle_file_is_read_with_its_exact_identity(tmp_path):
    path = _write_interface(tmp_path)
    loaded = load_bundles(tmp_path)
    assert loaded.blocked == frozenset()
    assert list(loaded.latest) == ["interface-error-handling"]
    bundle = loaded.latest["interface-error-handling"]
    assert bundle.identifier == "interface-error-handling"
    assert bundle.version == "1.0"
    assert bundle.status == "draft"
    assert bundle.title == "Interface error handling"
    assert bundle.scope.entities == ("Router-*",)
    assert bundle.scope.purposes == ("diagnosis", "assurance")
    assert bundle.scope.ontology_classes == ("Interface",)
    assert bundle.source_references == ("repo:design/event-store.md",)
    assert bundle.linked_actions == ("example-collect-interface-counters",)
    assert bundle.content_hash == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert bundle.embedding_text.startswith("Interface error handling\n")


def test_the_content_hash_is_stable_and_changes_with_one_byte(tmp_path):
    path = _write_interface(tmp_path)
    first = load_bundles(tmp_path).latest["interface-error-handling"].content_hash
    assert load_bundles(tmp_path).latest["interface-error-handling"].content_hash == first
    path.write_bytes(path.read_bytes().replace(b"counters", b"counterz"))
    assert load_bundles(tmp_path).latest["interface-error-handling"].content_hash != first


def test_a_matching_request_gets_a_match_and_no_other_does(tmp_path):
    _write_interface(tmp_path)
    bundle = load_bundles(tmp_path).latest["interface-error-handling"]
    assert applicable(bundle, device="Router-A", interface="GigabitEthernet0/0/0",
                      purpose="diagnosis") == Match(
        entities=("Router-*",), purpose="diagnosis", ontology_class="Interface")
    assert applicable(bundle, device="Router-A", interface=None, purpose="diagnosis") is None
    assert applicable(bundle, device="Router-A", interface="GigabitEthernet0/0/0",
                      purpose="reporting") is None
    assert applicable(bundle, device="Switch-1", interface="GigabitEthernet0/0/0",
                      purpose="diagnosis") is None


def test_version_order_is_numeric_and_the_public_name_is_the_private_function():
    assert version_key is _version_key
    assert version_key("1.10") > version_key("1.2")


# ---- refusals (Task 2): each file gets a fixed reason and blocks its identifier ----

ID = "sample-bundle"
SENTINEL = "SENTINEL-FILE-TEXT-7731"


def _good(**kw) -> str:
    return bundle_text(ID, "1.0", **kw)


def _cases():
    good = _good()
    return [
        ("duplicate key", good.replace("status: draft\n", "status: draft\nstatus: draft\n").encode(),
         "duplicate key"),
        ("unquoted version", good.replace('version: "1.0"', "version: 1.0").encode(),
         "invalid front matter"),
        ("status no", good.replace("status: draft", "status: no").encode(), "invalid front matter"),
        ("unknown status", good.replace("status: draft", "status: deprecated").encode(),
         "invalid front matter"),
        ("bare date", good.replace('title: "Interface error handling"', "title: 2026-11-06").encode(),
         "invalid front matter"),
        ("unknown field", good.replace("source_references", "extra: 1\nsource_references").encode(),
         "invalid front matter"),
        ("byte order mark", b"\xef\xbb\xbf" + good.encode(), "byte order mark"),
        ("carriage return", good.replace("\n", "\r\n").encode(), "carriage return"),
        ("not utf-8", good.encode() + b"\xff\xfe", "not utf-8"),
        ("too large", good.encode() + b"a" * MAX_FILE_BYTES, "too large"),
        ("no front matter", b"Just text, no front matter.\n", "no front matter"),
        ("unterminated front matter", good.replace("\n---\n", "\n").encode(), "no front matter"),
        ("not yaml", good.replace("scope:", "scope: [unclosed").encode(), "not yaml"),
        ("impossible date", good.replace("scope:", "reviewed: 2026-13-45\nscope:").encode(), "not yaml"),
        ("impossible date, day", good.replace("scope:", "reviewed: 2026-02-31\nscope:").encode(), "not yaml"),
        ("empty front matter", b"---\n---\nbody\n", "invalid front matter"),
        ("whitespace body", (good.split("\n---\n")[0] + "\n---\n   \n\n").encode(), "no body"),
        ("body too long", _good(body="x" * (MAX_BODY_CHARS + 1)).encode(), "body too long"),
        ("path mismatch directory", bundle_text("other-name", "1.0").encode(), "path mismatch"),
        ("entity question mark", _good(entities=["Router-?"]).encode(), "invalid front matter"),
        ("entity inner star", _good(entities=["Rou*ter"]).encode(), "invalid front matter"),
        ("entity empty list", _good(entities=[]).encode(), "invalid front matter"),
        ("unknown purpose", _good(purposes=["admin"]).encode(), "invalid front matter"),
        ("url source", _good(source_references=["http://x"]).encode(), "invalid front matter"),
        ("unknown ontology class", _good(ontology_classes=["Link"]).encode(), "invalid front matter"),
        ("long title", _good(title="t" * 121).encode(), "invalid front matter"),
        ("non-ascii title", _good(title="café").encode(), "invalid front matter"),
    ]


@pytest.mark.parametrize("name,data,reason", _cases(), ids=[c[0] for c in _cases()])
def test_a_refused_file_blocks_its_identifier_with_a_fixed_reason(tmp_path, caplog, name, data, reason):
    write_raw(tmp_path, ID, "1.0", data)
    with caplog.at_level(logging.WARNING):
        loaded = load_bundles(tmp_path)
    assert ID not in loaded.latest
    assert loaded.blocked == frozenset({ID})
    assert f"knowledge bundle refused: {ID}: {reason}" in caplog.text
    with pytest.raises(LoadError) as raised:
        parse_bundle(tmp_path / "bundles" / ID / "1.0.md", data)
    assert raised.value.reason == reason and str(raised.value) == reason
    assert reason in LOAD_REASONS


def test_a_stem_that_differs_from_the_front_matter_is_a_path_mismatch(tmp_path):
    write_raw(tmp_path, ID, "2.0", _good().encode())
    assert load_bundles(tmp_path).blocked == frozenset({ID})


def test_a_stem_that_is_not_a_version_is_an_invalid_version(tmp_path, caplog):
    write_raw(tmp_path, ID, "latest", bundle_text(ID, "latest").encode())
    with caplog.at_level(logging.WARNING):
        loaded = load_bundles(tmp_path)
    assert loaded.blocked == frozenset({ID})
    assert f"knowledge bundle refused: {ID}: invalid version" in caplog.text


def test_a_symlinked_file_is_refused(tmp_path, caplog):
    real = tmp_path / "elsewhere.md"
    real.write_bytes(_good().encode())
    link = tmp_path / "bundles" / ID / "1.0.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    with caplog.at_level(logging.WARNING):
        loaded = load_bundles(tmp_path)
    assert loaded.blocked == frozenset({ID}) and loaded.latest == {}
    assert f"knowledge bundle refused: {ID}: symlink" in caplog.text


def test_a_file_that_is_not_markdown_blocks_the_identifier(tmp_path, caplog):
    write_bundle(tmp_path, ID, "1.0")
    (tmp_path / "bundles" / ID / "notes.txt").write_bytes(b"x")
    with caplog.at_level(logging.WARNING):
        loaded = load_bundles(tmp_path)
    assert loaded.blocked == frozenset({ID}) and ID not in loaded.latest
    assert f"knowledge bundle refused: {ID}: not a bundle file" in caplog.text


def test_a_subdirectory_inside_a_bundle_directory_blocks_the_identifier(tmp_path):
    write_bundle(tmp_path, ID, "1.0")
    (tmp_path / "bundles" / ID / "nested").mkdir()
    assert load_bundles(tmp_path).blocked == frozenset({ID})


# ---- versions and withdrawn ----

def test_one_point_ten_is_later_than_one_point_two(tmp_path):
    write_bundle(tmp_path, ID, "1.2")
    write_bundle(tmp_path, ID, "1.10", title="Newer title")
    loaded = load_bundles(tmp_path)
    assert loaded.latest[ID].version == "1.10"
    assert loaded.latest[ID].title == "Newer title"


def test_two_versions_that_compare_equal_block_the_identifier(tmp_path, caplog):
    write_bundle(tmp_path, ID, "1.0")
    write_bundle(tmp_path, ID, "1.00")
    with caplog.at_level(logging.WARNING):
        loaded = load_bundles(tmp_path)
    assert loaded.blocked == frozenset({ID}) and ID not in loaded.latest
    assert f"knowledge bundle refused: {ID}: duplicate version" in caplog.text


def test_an_invalid_newer_version_blocks_the_identifier_so_the_older_is_not_served(tmp_path):
    write_bundle(tmp_path, ID, "1.0")
    write_raw(tmp_path, ID, "2.0", b"broken\n")
    loaded = load_bundles(tmp_path)
    assert ID not in loaded.latest
    assert loaded.blocked == frozenset({ID})


def test_a_withdrawn_latest_version_removes_the_identifier_without_blocking(tmp_path):
    write_bundle(tmp_path, ID, "1.0")
    write_bundle(tmp_path, ID, "2.0", status="withdrawn")
    loaded = load_bundles(tmp_path)
    assert ID not in loaded.latest
    assert loaded.blocked == frozenset()


def test_a_withdrawn_older_version_does_not_hide_a_newer_one(tmp_path):
    write_bundle(tmp_path, ID, "1.0", status="withdrawn")
    write_bundle(tmp_path, ID, "2.0", status="approved")
    assert load_bundles(tmp_path).latest[ID].status == "approved"


def test_one_blocked_identifier_does_not_stop_another_from_loading(tmp_path):
    write_raw(tmp_path, ID, "1.0", b"broken\n")
    write_bundle(tmp_path, "latency-goal-investigation", "1.0")
    loaded = load_bundles(tmp_path)
    assert list(loaded.latest) == ["latency-goal-investigation"]
    assert loaded.blocked == frozenset({ID})


# ---- tree limits ----

def test_a_tree_over_the_file_cap_is_refused_before_any_file_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "MAX_FILES", 3)
    for index in range(4):
        write_bundle(tmp_path, f"bundle-{index}", "1.0")
    read = []
    monkeypatch.setattr(loader, "_read", lambda path: read.append(path))
    with pytest.raises(LoadError) as raised:
        load_bundles(tmp_path)
    assert raised.value.reason == "too many files" and read == []


def test_a_root_without_a_bundles_directory_is_refused(tmp_path):
    with pytest.raises(LoadError) as raised:
        load_bundles(tmp_path)
    assert raised.value.reason == "no bundles directory"


def test_a_symlinked_bundle_directory_is_skipped_with_a_fixed_line(tmp_path, caplog):
    elsewhere = tmp_path / "elsewhere"
    write_bundle(elsewhere, ID, "1.0")
    (tmp_path / "bundles").mkdir()
    (tmp_path / "bundles" / ID).symlink_to(elsewhere / "bundles" / ID, target_is_directory=True)
    with caplog.at_level(logging.WARNING):
        loaded = load_bundles(tmp_path)
    assert loaded.latest == {} and loaded.blocked == frozenset()
    assert "knowledge entry skipped: symlink" in caplog.text
    assert ID not in caplog.text


def test_a_stray_file_and_a_badly_named_directory_in_bundles_are_skipped(tmp_path, caplog):
    write_bundle(tmp_path, ID, "1.0")
    (tmp_path / "bundles" / "README.md").write_bytes(b"x")
    (tmp_path / "bundles" / "Bad_Name").mkdir()
    with caplog.at_level(logging.WARNING):
        loaded = load_bundles(tmp_path)
    assert list(loaded.latest) == [ID] and loaded.blocked == frozenset()
    assert "Bad_Name" not in caplog.text and "README" not in caplog.text


# ---- no file text in a log line or an exception ----

SENTINEL_FILES = {
    "duplicate": f"---\nidentifier: {ID}\nidentifier: {SENTINEL}\n---\nbody\n",
    "not yaml": f"---\nscope: [{SENTINEL}\n---\nbody\n",
    "invalid value": _good(title=SENTINEL + "é").replace("é", "é"),
    "bad purpose": _good(purposes=[SENTINEL]),
    "unknown field": _good().replace("source_references", f"{SENTINEL}: 1\nsource_references"),
    "no front matter": SENTINEL + "\n",
    "bad entity": _good(entities=[SENTINEL + "?"]),
}


@pytest.mark.parametrize("name", list(SENTINEL_FILES))
def test_file_text_reaches_neither_a_log_line_nor_an_exception(tmp_path, caplog, name):
    data = SENTINEL_FILES[name].encode("utf-8")
    write_raw(tmp_path, ID, "1.0", data)
    with caplog.at_level(logging.DEBUG):
        loaded = load_bundles(tmp_path)
    assert loaded.blocked == frozenset({ID})
    assert SENTINEL not in caplog.text
    with pytest.raises(LoadError) as raised:
        parse_bundle(tmp_path / "bundles" / ID / "1.0.md", data)
    assert SENTINEL not in str(raised.value) and SENTINEL not in repr(raised.value)
    assert raised.value.__cause__ is None


def test_the_loader_never_logs_a_traceback():
    source = (loader.__file__ and open(loader.__file__).read())
    assert "exc_info=" not in source and ".exception(" not in source


# ---- the action catalogue file (Task 3) ----

from artefact_governance.action_catalogue import ActionCatalogue  # noqa: E402
from okf_retrieval.loader import CATALOGUE_FILE, MAX_TEMPLATES, load_catalogue  # noqa: E402
from tests.okf_helpers import TEMPLATE, write_catalogue  # noqa: E402

UNAPPROVED = {**TEMPLATE, "template_id": "example-restart-interface", "approved": False,
              "expected_outcome": "interface returns to forwarding"}


def test_the_catalogue_file_seeds_the_catalogue_with_approved_latest_semantics(tmp_path):
    write_catalogue(tmp_path, [TEMPLATE, UNAPPROVED])
    catalogue = load_catalogue(tmp_path)
    assert isinstance(catalogue, ActionCatalogue)
    template = catalogue.get("example-collect-interface-counters")
    assert template.version == "1.0" and template.scope_pattern == "Router-*"
    assert template.preconditions == ("interface exists in the topology",)
    assert template.expected_outcome == "interface counters are read and recorded"
    assert template.recovery == "none needed; the action only reads"
    assert template.approved is True
    assert catalogue.get("example-restart-interface") is None


def test_a_newer_unapproved_version_does_not_hide_the_older_approved_one(tmp_path):
    write_catalogue(tmp_path, [TEMPLATE, {**TEMPLATE, "version": "2.0", "approved": False}])
    assert load_catalogue(tmp_path).get("example-collect-interface-counters").version == "1.0"


def test_a_missing_catalogue_file_is_an_empty_catalogue_and_one_fixed_line(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        catalogue = load_catalogue(tmp_path)
    assert catalogue.get("example-collect-interface-counters") is None
    assert caplog.text.count("knowledge action catalogue missing") == 1


def _catalogue_cases():
    good = None
    many = [{**TEMPLATE, "version": f"1.{n}"} for n in range(MAX_TEMPLATES + 1)]
    return [
        ("duplicate key", b"templates: []\ntemplates: []\n"),
        ("not yaml", b"templates: [unclosed\n"),
        ("not a mapping", b"- a\n- b\n"),
        ("no templates key", b"other: []\n"),
        ("unknown top key", b"templates: []\nextra: 1\n"),
        ("unknown field", ("add", {**TEMPLATE, "extra": 1})),
        ("missing field", ("add", {k: v for k, v in TEMPLATE.items() if k != "recovery"})),
        ("string approved", ("add", {**TEMPLATE, "approved": "true"})),
        ("equal versions", ("many", [TEMPLATE, {**TEMPLATE, "version": "1.00"}])),
        ("bad scope pattern", ("add", {**TEMPLATE, "scope_pattern": "Router-?"})),
        ("bad template id", ("add", {**TEMPLATE, "template_id": "Bad_ID"})),
        ("bad version", ("add", {**TEMPLATE, "version": "one"})),
        ("too many templates", ("many", many)),
        ("too many preconditions", ("add", {**TEMPLATE, "preconditions": ["x"] * 11})),
        ("non-ascii text", ("add", {**TEMPLATE, "recovery": "café"})),
        ("byte order mark", ("bytes", b"\xef\xbb\xbf" + b"templates: []\n")),
        ("carriage return", ("bytes", b"templates: []\r\n")),
        ("not utf-8", ("bytes", b"templates: []\n\xff")),
        ("too large", ("bytes", b"templates: []\n# " + b"a" * MAX_FILE_BYTES)),
    ]


def _catalogue_bytes(tmp_path, spec):
    if isinstance(spec, bytes):
        return spec
    kind, value = spec
    if kind == "bytes":
        return value
    write_catalogue(tmp_path, value if kind == "many" else [value])
    return (tmp_path / CATALOGUE_FILE).read_bytes()


@pytest.mark.parametrize("name", [c[0] for c in _catalogue_cases()])
def test_a_bad_catalogue_file_is_refused_with_the_fixed_reason(tmp_path, name):
    spec = dict(_catalogue_cases())[name]
    (tmp_path / CATALOGUE_FILE).write_bytes(_catalogue_bytes(tmp_path, spec))
    with pytest.raises(LoadError) as raised:
        load_catalogue(tmp_path)
    assert raised.value.reason == "invalid catalogue" and str(raised.value) == "invalid catalogue"
    assert raised.value.__cause__ is None


def test_an_unquoted_catalogue_version_is_refused(tmp_path):
    write_catalogue(tmp_path, [TEMPLATE])
    path = tmp_path / CATALOGUE_FILE
    path.write_bytes(path.read_bytes().replace(b'"1.0"', b"1.0"))
    with pytest.raises(LoadError) as raised:
        load_catalogue(tmp_path)
    assert raised.value.reason == "invalid catalogue"


def test_a_symlinked_catalogue_file_is_refused(tmp_path):
    real = tmp_path / "elsewhere.yaml"
    real.write_bytes(b"templates: []\n")
    (tmp_path / CATALOGUE_FILE).symlink_to(real)
    with pytest.raises(LoadError) as raised:
        load_catalogue(tmp_path)
    assert raised.value.reason == "invalid catalogue"


def test_catalogue_file_text_is_not_in_the_exception(tmp_path):
    write_catalogue(tmp_path, [{**TEMPLATE, "recovery": SENTINEL + "é"}])
    with pytest.raises(LoadError) as raised:
        load_catalogue(tmp_path)
    assert SENTINEL not in str(raised.value) and SENTINEL not in repr(raised.value)


def test_the_loader_imports_no_action_execution_package():
    import ast
    tree = ast.parse(open(loader.__file__).read())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
        elif isinstance(node, ast.Import):
            names += [a.name for a in node.names]
    assert not [n for n in names if n.split(".")[0] in {"action_lifecycle", "governance_gate", "loop_coordination"}]
