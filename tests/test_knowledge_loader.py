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
