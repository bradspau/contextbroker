import pytest
from artefact_governance.action_catalogue import ActionCatalogue, ActionTemplate
from artefact_governance.model_registry import ModelRegistry


def tpl(version="1.0", approved=True, tid="t"):
    return ActionTemplate(tid, version, "site/*", ("p",), "ok", "rollback", approved)


def test_unapproved_invisible():
    c = ActionCatalogue([tpl(approved=False)])
    assert c.get("t") is None


def test_approved_visible_and_unknown_none():
    c = ActionCatalogue()
    c.add(tpl())
    assert c.get("t").version == "1.0"
    assert c.get("zzz") is None


def test_latest_approved_version_numeric():
    c = ActionCatalogue([tpl("1.2"), tpl("1.10"), tpl("2.0", approved=False)])
    assert c.get("t").version == "1.10"


def test_template_frozen():
    with pytest.raises(Exception):
        tpl().approved = False


def test_model_registry():
    r = ModelRegistry()
    assert r.status("m", "1") is None
    r.register("m", "1", "candidate")
    assert r.status("m", "1") == "candidate"
    r.register("m", "1", "approved")
    assert r.status("m", "1") == "approved"
    r.register("m", "1", "retired")
    assert r.status("m", "1") == "retired"


def test_bad_status():
    with pytest.raises(ValueError):
        ModelRegistry().register("m", "1", "bogus")


@pytest.mark.parametrize("v", ["", "1.", ".1", "1..2", "v1", "1.0-rc", "١.٢", "1.0\n", " 1"])
def test_strict_version_rejected(v):
    with pytest.raises(ValueError):
        ActionCatalogue().add(tpl(v))


def test_duplicate_template_version_rejected():
    c = ActionCatalogue([tpl("1.0")])
    with pytest.raises(ValueError):
        c.add(tpl("1.0"))
