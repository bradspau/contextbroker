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


@pytest.fixture(autouse=True)
def _fuseki_credential(monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "testpassword0")


def _write(tmp_path, text):
    d = tmp_path / "tbox"
    d.mkdir()
    (d / "bad.ttl").write_text(text)
    return d


def test_load_ontologies_posts_nothing_on_violation(tmp_path):
    d = _write(tmp_path, _old_binding(forIntent=False))
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


def _old_binding(forIntent=True, forMetric=True, iface=True, pins=('"0.0"^^xsd:double',)):
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


IF_A = ":if-router-a-ge000"
IF_B = ":if-router-b-ge001"


def _fluent_binding(
    *,
    forIntent=True,
    forMetric=True,
    goal=True,
    placement=True,
    link=False,
    goal_start='"2026-01-01T00:00:00Z"^^xsd:dateTime',
    goal_end=None,
    goal_recorded='"2026-01-01T00:00:00Z"^^xsd:dateTime',
    goal_superseded=None,
    goal_pins=('"0.0"^^xsd:double',),
    place_iface=(IF_A,),
    link_ifaces=(IF_A, IF_B),
    link_of=True,
):
    """A fluent-shaped binding file: a binding node plus the fluents it points at."""
    bparts = []
    if forIntent:
        bparts.append(':forIntent "intent-1"')
    if forMetric:
        bparts.append(':forMetric "http://example.org/metric/input_drops"')
    if goal:
        bparts.append(":hasGoal :g1")
    if placement:
        bparts.append(":hasPlacement :p1")
    text = BINDING_PREFIXES + ":b1 a :ServiceBinding ; " + " ; ".join(bparts) + " .\n"
    if goal:
        gparts = [":pinnedBound " + p for p in goal_pins]
        if goal_start:
            gparts.append(f":validStart {goal_start}")
        if goal_recorded:
            gparts.append(f":recordedAt {goal_recorded}")
        if goal_end:
            gparts.append(f":validEnd {goal_end}")
        if goal_superseded:
            gparts.append(f":supersededAt {goal_superseded}")
        text += ":g1 a :GoalFluent ; " + " ; ".join(gparts) + " .\n"
    if placement:
        pparts = [':validStart "2026-01-01T00:00:00Z"^^xsd:dateTime',
                  ':recordedAt "2026-01-01T00:00:00Z"^^xsd:dateTime']
        pparts += [f":onInterface {i}" for i in place_iface]
        text += ":p1 a :PlacementFluent ; " + " ; ".join(pparts) + " .\n"
    if link:
        lparts = [':validStart "2026-01-01T00:00:00+00:00"^^xsd:dateTime',
                  ':recordedAt "2026-01-01T00:00:00+00:00"^^xsd:dateTime']
        if link_of:
            lparts.append(":ofLink :link-router-a-router-b")
        lparts += [f":hasInterface {i}" for i in link_ifaces]
        text += ":l1 a :LinkFluent ; " + " ; ".join(lparts) + " .\n"
    return text


def _abox_file(tmp_path, text, name="b.ttl", marker=True):
    """An ABox fixture; by default it starts with the marker that bind_intent writes."""
    d = tmp_path / "abox"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_text((loader.ABOX_MARKER + "\n" if marker else "") + text)
    return f


def _check(tmp_path, text):
    return validate_shapes(_abox_file(tmp_path, text), SHAPES)


def test_fluent_binding_shape_accepts_complete_binding(tmp_path):
    ok, report = _check(tmp_path, _fluent_binding(link=True))
    assert ok is True, report


def test_open_goal_fluent_conforms(tmp_path):
    ok, report = _check(tmp_path, _fluent_binding())
    assert ok is True, report


def test_closed_goal_fluent_conforms(tmp_path):
    text = _fluent_binding(goal_end='"2026-02-01T00:00:00Z"^^xsd:dateTime')
    ok, report = _check(tmp_path, text)
    assert ok is True, report


def test_superseded_goal_fluent_conforms(tmp_path):
    text = _fluent_binding(goal_superseded='"2026-03-01T00:00:00Z"^^xsd:dateTime')
    ok, report = _check(tmp_path, text)
    assert ok is True, report


def test_offset_zone_conforms(tmp_path):
    text = _fluent_binding(goal_start='"2026-01-01T00:00:00+00:00"^^xsd:dateTime',
                           goal_recorded='"2026-01-01T00:00:00+00:00"^^xsd:dateTime')
    ok, report = _check(tmp_path, text)
    assert ok is True, report


def test_valid_end_equal_to_start_is_rejected(tmp_path):
    text = _fluent_binding(goal_end='"2026-01-01T00:00:00Z"^^xsd:dateTime')
    ok, report = _check(tmp_path, text)
    assert ok is False
    assert "validStart" in report


def test_valid_end_before_start_is_rejected(tmp_path):
    text = _fluent_binding(goal_end='"2025-12-31T00:00:00Z"^^xsd:dateTime')
    ok, report = _check(tmp_path, text)
    assert ok is False
    assert "validStart" in report


def test_superseded_before_recorded_is_rejected(tmp_path):
    text = _fluent_binding(goal_superseded='"2025-12-31T00:00:00Z"^^xsd:dateTime')
    ok, report = _check(tmp_path, text)
    assert ok is False
    assert "recordedAt" in report


def test_timestamp_without_zone_is_rejected(tmp_path):
    text = _fluent_binding(goal_start='"2026-01-01T00:00:00"^^xsd:dateTime')
    ok, report = _check(tmp_path, text)
    assert ok is False
    assert "validStart" in report


def test_plain_string_time_is_rejected(tmp_path):
    text = _fluent_binding(goal_start='"2026-01-01T00:00:00Z"')
    ok, report = _check(tmp_path, text)
    assert ok is False
    assert "validStart" in report


def test_ill_typed_datetime_is_rejected(tmp_path):
    text = _fluent_binding(goal_start='"yesterday"^^xsd:dateTime')
    ok, report = _check(tmp_path, text)
    assert ok is False
    assert "validStart" in report


def test_goal_fluent_without_recorded_at_is_rejected(tmp_path):
    ok, report = _check(tmp_path, _fluent_binding(goal_recorded=None))
    assert ok is False
    assert "recordedAt" in report


def test_goal_fluent_without_valid_start_is_rejected(tmp_path):
    ok, report = _check(tmp_path, _fluent_binding(goal_start=None))
    assert ok is False
    assert "validStart" in report


def test_goal_fluent_with_two_goals_is_rejected(tmp_path):
    text = _fluent_binding(goal_pins=('"0.0"^^xsd:double', '"1.0"^^xsd:double'))
    ok, report = _check(tmp_path, text)
    assert ok is False
    assert "pinnedBound" in report


def test_integer_goal_is_rejected(tmp_path):
    ok, report = _check(tmp_path, _fluent_binding(goal_pins=("5",)))
    assert ok is False
    assert "pinnedBound" in report


def test_non_numeric_goal_is_rejected(tmp_path):
    ok, report = _check(tmp_path, _fluent_binding(goal_pins=('"abc"',)))
    assert ok is False
    assert "pinnedBound" in report


def test_placement_fluent_without_interface_is_rejected(tmp_path):
    ok, report = _check(tmp_path, _fluent_binding(place_iface=()))
    assert ok is False
    assert "onInterface" in report


def test_placement_fluent_with_two_interfaces_is_rejected(tmp_path):
    ok, report = _check(tmp_path, _fluent_binding(place_iface=(IF_A, IF_B)))
    assert ok is False
    assert "onInterface" in report


def test_link_fluent_with_one_interface_is_rejected(tmp_path):
    ok, report = _check(tmp_path, _fluent_binding(link=True, link_ifaces=(IF_A,)))
    assert ok is False
    assert "hasInterface" in report


def test_link_fluent_without_link_is_rejected(tmp_path):
    ok, report = _check(tmp_path, _fluent_binding(link=True, link_of=False))
    assert ok is False
    assert "ofLink" in report


def test_binding_without_intent_is_rejected(tmp_path):
    ok, report = _check(tmp_path, _fluent_binding(forIntent=False))
    assert ok is False
    assert "forIntent" in report


def test_binding_without_metric_is_rejected(tmp_path):
    ok, report = _check(tmp_path, _fluent_binding(forMetric=False))
    assert ok is False
    assert "forMetric" in report


def test_binding_may_carry_only_the_fluent_that_changed(tmp_path):
    ok, report = _check(tmp_path, _fluent_binding(placement=False))
    assert ok is True, report


def test_a_binding_that_still_carries_the_goal_does_not_conform(tmp_path):
    ok, report = _check(tmp_path, _old_binding(iface=False))
    assert ok is False
    assert "pinnedBound" in report


def test_a_binding_that_still_carries_the_interface_does_not_conform(tmp_path):
    ok, report = _check(tmp_path, _old_binding(pins=()))
    assert ok is False
    assert "onInterface" in report


def test_a_fluent_shaped_binding_still_conforms_after_the_retired_shape_is_refused(tmp_path):
    ok, report = _check(tmp_path, _fluent_binding(link=True))
    assert ok is True, report


def test_load_ontologies_refuses_an_abox_file_in_the_old_shape_and_posts_nothing(tmp_path):
    _abox_file(tmp_path, _old_binding(), name="intent-1.ttl")
    with patch("requests.post") as post:
        with pytest.raises(ValueError):
            load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=tmp_path / "abox")
    post.assert_not_called()


def test_abox_failure_loads_nothing(tmp_path):
    _abox_file(tmp_path, _fluent_binding(goal_end='"2025-12-31T00:00:00Z"^^xsd:dateTime'))
    with patch("requests.post") as post:
        with pytest.raises(ValueError, match="validStart"):
            load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=tmp_path / "abox")
    post.assert_not_called()


def test_naive_time_in_abox_loads_nothing(tmp_path):
    _abox_file(tmp_path, _fluent_binding(goal_start='"2026-01-01T00:00:00"^^xsd:dateTime'))
    with patch("requests.post") as post:
        with pytest.raises(ValueError, match="SHACL validation failed; nothing loaded"):
            load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=tmp_path / "abox")
    post.assert_not_called()


def test_abox_dir_absent_is_not_an_error(tmp_path):
    resp = Mock()
    resp.raise_for_status = Mock()
    with patch("requests.post", return_value=resp) as post:
        load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=tmp_path / "nope")
    assert post.call_count == len(list(TBOX_DIR.glob("*.ttl")))


def test_abox_files_are_posted_after_validation(tmp_path):
    _abox_file(tmp_path, _fluent_binding(link=True))
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


def _ok_post():
    resp = Mock()
    resp.raise_for_status = Mock()
    return patch("ingestion.load_ontologies.requests.post", return_value=resp)


def test_abox_static_allow_list_names_only_the_demo_topology():
    assert loader.ABOX_STATIC_ALLOWLIST == ("topology-demo.ttl",)


def test_abox_files_are_not_subject_to_the_tbox_allow_list(tmp_path):
    # a tool-written file loads whatever it is called
    _abox_file(tmp_path, _fluent_binding(), name="not-on-the-list.ttl")
    with _ok_post() as post:
        load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=tmp_path / "abox")
    assert post.call_count == len(list(TBOX_DIR.glob("*.ttl"))) + 1


def test_abox_file_on_the_static_list_loads_without_the_marker(tmp_path):
    demo = (ROOT / "ontologies" / "abox" / "topology-demo.ttl").read_text()
    _abox_file(tmp_path, demo, name="topology-demo.ttl", marker=False)
    with _ok_post() as post:
        load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=tmp_path / "abox")
    assert post.call_count == len(list(TBOX_DIR.glob("*.ttl"))) + 1


def test_abox_file_without_the_marker_is_refused_before_validation_and_post(tmp_path):
    _abox_file(tmp_path, _fluent_binding(), name="licensed-ontology.ttl", marker=False)
    _abox_file(tmp_path, _fluent_binding(), name="intent-2.ttl")
    with patch("ingestion.load_ontologies.validate_shapes") as validate, \
            patch("ingestion.load_ontologies.requests.post") as post:
        with pytest.raises(ValueError, match="not written by the tooling; nothing loaded") as exc:
            load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=tmp_path / "abox")
    assert "licensed-ontology.ttl" in str(exc.value)
    assert "intent-2.ttl" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    validate.assert_not_called()
    post.assert_not_called()


def test_old_intent_file_without_the_marker_is_refused(tmp_path):
    _abox_file(tmp_path, _old_binding(), name="intent-1.ttl", marker=False)
    with patch("ingestion.load_ontologies.requests.post") as post:
        with pytest.raises(ValueError, match="not written by the tooling"):
            load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=tmp_path / "abox")
    post.assert_not_called()


def test_the_marker_admits_a_file_only_on_its_first_line(tmp_path):
    f = _abox_file(tmp_path, "", name="late.ttl", marker=False)
    f.write_text("# a comment\n" + loader.ABOX_MARKER + "\n" + _fluent_binding())
    with patch("ingestion.load_ontologies.requests.post") as post:
        with pytest.raises(ValueError, match="not written by the tooling") as exc:
            load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=tmp_path / "abox")
    assert "late.ttl" in str(exc.value)
    post.assert_not_called()


def test_a_symlink_in_the_abox_dir_to_a_file_outside_is_refused(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    target = outside / "x.ttl"
    target.write_text(loader.ABOX_MARKER + "\n" + _fluent_binding())
    d = tmp_path / "abox"
    d.mkdir()
    try:
        (d / "linked.ttl").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not supported on this platform")
    with patch("ingestion.load_ontologies.validate_shapes") as validate, \
            patch("ingestion.load_ontologies.requests.post") as post:
        with pytest.raises(ValueError, match="links outside the ABox directory; nothing loaded") as exc:
            load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=d)
    assert "linked.ttl" in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    validate.assert_not_called()
    post.assert_not_called()


def test_repository_abox_directory_loads_with_the_tbox_one_post_each_plus_the_demo():
    with _ok_post() as post:
        load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT, abox_dir=ROOT / "ontologies" / "abox")
    assert post.call_count == len(list(TBOX_DIR.glob("*.ttl"))) + 1


def test_caller_can_pass_its_own_allow_list(tmp_path):
    d = _tbox_with(tmp_path, ["mine.ttl"])
    resp = Mock()
    resp.raise_for_status = Mock()
    with patch("requests.post", return_value=resp) as post:
        load_ontologies(d, SHACL_DIR, ENDPOINT, allow=("mine.ttl",))
    assert post.call_count == 1


# --- the Fuseki credential comes only from the environment (ACC-03) ---

def test_missing_credential_exits_naming_the_variable_and_does_nothing_else(monkeypatch):
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
    with patch("ingestion.load_ontologies.validate_shapes") as validate, \
            patch("ingestion.load_ontologies.requests.post") as post:
        with pytest.raises(SystemExit) as exc:
            load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT)
        with pytest.raises(SystemExit) as exc_one:
            loader.load_ttl_to_fuseki(TBOX_DIR / "topology.ttl", ENDPOINT)
    assert "FUSEKI_ADMIN_PASSWORD" in str(exc.value)
    assert "FUSEKI_ADMIN_PASSWORD" in str(exc_one.value)
    assert isinstance(exc.value, loader.MissingCredential)
    validate.assert_not_called()
    post.assert_not_called()


def test_empty_credential_counts_as_unset(monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "")
    with patch("ingestion.load_ontologies.requests.post") as post:
        with pytest.raises(loader.MissingCredential):
            load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT)
    post.assert_not_called()


def test_loader_posts_with_the_environment_credential():
    resp = Mock()
    resp.raise_for_status = Mock()
    with patch("requests.post", return_value=resp) as post:
        load_ontologies(TBOX_DIR, SHACL_DIR, ENDPOINT)
    assert post.call_count >= 1
    for call in post.call_args_list:
        assert call.kwargs["auth"] == ("admin", "testpassword0")


def test_credential_is_read_on_each_call(monkeypatch):
    resp = Mock()
    resp.raise_for_status = Mock()
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "rotated0value")
    with patch("requests.post", return_value=resp) as post:
        loader.load_ttl_to_fuseki(TBOX_DIR / "topology.ttl", ENDPOINT)
    assert post.call_args.kwargs["auth"] == ("admin", "rotated0value")


def test_explicit_auth_is_still_honoured(monkeypatch):
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
    resp = Mock()
    resp.raise_for_status = Mock()
    with patch("requests.post", return_value=resp) as post:
        loader.load_ttl_to_fuseki(TBOX_DIR / "topology.ttl", ENDPOINT, auth=("admin", "explicit0"))
    assert post.call_args.kwargs["auth"] == ("admin", "explicit0")


def test_no_default_credential_in_signatures():
    import inspect
    for fn in (loader.load_ontologies, loader.load_ttl_to_fuseki):
        assert inspect.signature(fn).parameters["auth"].default is None


def test_main_exits_before_any_work_when_unset(monkeypatch):
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr("sys.argv", ["load_ontologies", "--endpoint", "http://127.0.0.1:9/x"])
    with patch("ingestion.load_ontologies.load_ontologies") as load, \
            patch("ingestion.load_ontologies.validate_shapes") as validate, \
            patch("ingestion.load_ontologies.requests.post") as post:
        with pytest.raises(SystemExit) as exc:
            loader.main()
    assert "FUSEKI_ADMIN_PASSWORD" in str(exc.value)
    load.assert_not_called()
    validate.assert_not_called()
    post.assert_not_called()


def test_fuseki_auth_reads_the_environment_each_call(monkeypatch):
    from broker_engine.sparql_resolver import fuseki_auth
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
    assert fuseki_auth() is None
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "")
    assert fuseki_auth() is None
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "abc123")
    assert fuseki_auth() == ("admin", "abc123")
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "def456")
    assert fuseki_auth() == ("admin", "def456")


# --- the query-only credential (bead contextbroker-oiq) ---


def test_the_read_pair_prefers_the_query_only_user_and_the_write_pair_is_admin_only(monkeypatch):
    from broker_engine.sparql_resolver import fuseki_auth, fuseki_write_auth
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "admin0value")
    monkeypatch.delenv("FUSEKI_READER_PASSWORD", raising=False)
    assert fuseki_auth() == ("admin", "admin0value")  # an operator shell with only the admin variable
    monkeypatch.setenv("FUSEKI_READER_PASSWORD", "reader0value")
    assert fuseki_auth() == ("reader", "reader0value")
    assert fuseki_write_auth() == ("admin", "admin0value")
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD")
    assert fuseki_auth() == ("reader", "reader0value")  # the API container: reader only
    assert fuseki_write_auth() is None
    monkeypatch.setenv("FUSEKI_READER_PASSWORD", "")
    assert fuseki_auth() is None


def test_a_command_that_writes_never_accepts_the_query_only_credential(monkeypatch):
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("FUSEKI_READER_PASSWORD", "reader0value")
    with pytest.raises(loader.MissingCredential) as exc:
        loader.fuseki_credentials()
    assert "FUSEKI_ADMIN_PASSWORD" in str(exc.value) and "reader0value" not in str(exc.value)
    assert loader.fuseki_read_credentials() == ("reader", "reader0value")


def test_a_command_that_reads_names_both_variables_when_neither_is_set(monkeypatch):
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("FUSEKI_READER_PASSWORD", raising=False)
    with pytest.raises(loader.MissingCredential) as exc:
        loader.fuseki_read_credentials()
    assert "FUSEKI_READER_PASSWORD" in str(exc.value) and "FUSEKI_ADMIN_PASSWORD" in str(exc.value)


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
    return _old_binding()  # a complete :ServiceBinding pointing at an existing interface


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
                 "TopologicalLink", "Interface", "hasInterface", "onDevice", "interfaceName",
                 "GoalFluent", "PlacementFluent", "LinkFluent", "hasGoal", "hasPlacement",
                 "ofLink", "validStart", "validEnd", "recordedAt", "supersededAt"):
        assert (rdflib.URIRef(NETWORK_NS + kept), None, None) in g, kept


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


# --- the fluent vocabulary (TEMP-06, D-01, D-02, D-19) ---

def _tbox_graph():
    import rdflib
    return rdflib.Graph().parse(TBOX_DIR / "topology.ttl", format="turtle")


def test_fluent_vocabulary_domains_and_ranges():
    import rdflib
    from rdflib import OWL, RDFS, XSD
    N = rdflib.Namespace(NETWORK_NS)
    g = _tbox_graph()
    for cls in ("GoalFluent", "PlacementFluent", "LinkFluent"):
        assert (N[cls], rdflib.RDF.type, OWL.Class) in g, cls
        assert g.value(N[cls], RDFS.label) is not None, cls
        assert g.value(N[cls], RDFS.comment) is not None, cls
    objects = {
        "hasGoal": (N.ServiceBinding, N.GoalFluent),
        "hasPlacement": (N.ServiceBinding, N.PlacementFluent),
        "ofLink": (N.LinkFluent, N.TopologicalLink),
    }
    for name, (dom, rng) in objects.items():
        assert (N[name], rdflib.RDF.type, OWL.ObjectProperty) in g, name
        assert g.value(N[name], RDFS.domain) == dom, name
        assert g.value(N[name], RDFS.range) == rng, name
    for name in ("validStart", "validEnd", "recordedAt", "supersededAt"):
        assert (N[name], rdflib.RDF.type, OWL.DatatypeProperty) in g, name
        assert g.value(N[name], RDFS.range) == XSD.dateTime, name
        assert g.value(N[name], RDFS.domain) is None, name
        assert g.value(N[name], RDFS.comment) is not None, name
    assert g.value(N.pinnedBound, RDFS.domain) == N.GoalFluent
    assert g.value(N.pinnedBound, RDFS.range) == XSD.double
    assert g.value(N.onInterface, RDFS.domain) == N.PlacementFluent
    assert g.value(N.onInterface, RDFS.range) == N.Interface
    assert g.value(N.hasInterface, RDFS.domain) == N.LinkFluent
    assert g.value(N.hasInterface, RDFS.range) == N.Interface


def test_service_binding_comment_names_goal_not_bound():
    import rdflib
    N = rdflib.Namespace(NETWORK_NS)
    comment = str(_tbox_graph().value(N.ServiceBinding, rdflib.RDFS.comment))
    assert "goal" in comment
    assert "bound" not in comment.lower().replace("pinnedbound", "")


def test_retire_migration_keeps_fluents():
    from rdflib.compare import isomorphic
    fluents = _fluent_binding(link=True)
    tbox = (TBOX_DIR / "topology.ttl").read_text()
    g = _graph(tbox, fluents)
    expected = _graph(tbox, fluents)
    before = len(g)
    g.update(MIGRATION.read_text())
    assert len(g) == before
    assert isomorphic(g, expected)
