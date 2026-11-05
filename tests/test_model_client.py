from datetime import datetime, timezone
from pathlib import Path

import pytest

import investigation_workers
from artefact_governance.model_registry import ModelRegistry
from broker_engine.pruner import PrunedContext
from data_contracts.envelope import ContextEnvelope, ProvenanceRecord, Quality, StateCategory
from investigation_workers.model_client import (
    ModelNotApproved,
    RouterStub,
    ThresholdLqmStub,
    ensure_approved,
)

T1 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)


def env(entity="r1", value=1.0, at=T1, stale=False, missing=False, conflicts=None):
    return ContextEnvelope(entity, StateCategory.OBSERVED, value, "tsdb", at,
                           Quality(stale=stale, missing=missing, conflicts=conflicts or []))


def registry(status="approved"):
    r = ModelRegistry()
    if status:
        r.register("lqm-threshold-stub", "0.1", status)
        r.register("system1-router-stub", "0.1", status)
    return r


def lqm(status="approved", threshold=0.0):
    return ThresholdLqmStub(registry(status), threshold=threshold, now=lambda: NOW)


def test_output_is_inferred_with_model_provenance():
    out = lqm().predict([env(value=5.0)])
    assert out.state == StateCategory.INFERRED
    model = out.provenance[0]
    assert model == ProvenanceRecord("lqm-threshold-stub", "lqm-threshold-stub@0.1", NOW, NOW)
    assert out.source == "lqm-threshold-stub"
    assert out.observed_at == NOW


@pytest.mark.parametrize("status", [None, "candidate", "retired"])
def test_unapproved_model_refused(status):
    with pytest.raises(ModelNotApproved):
        lqm(status).predict([env()])
    assert issubclass(ModelNotApproved, PermissionError)


def test_router_unapproved_refused():
    r = RouterStub(registry("candidate"))
    with pytest.raises(PermissionError):
        r.route(PrunedContext(kept=[], dropped_count=0))


def test_ensure_approved_ok():
    ensure_approved(registry(), lqm())


def test_no_usable_inputs_gives_missing_envelope():
    out = lqm().predict([env(missing=True, value=None), env(stale=True)])
    assert out.value is None
    assert out.quality.missing is True
    assert out.quality.stale is False


def test_empty_inputs():
    out = lqm().predict([])
    assert out.entity_id == "unknown"
    assert out.value is None and out.quality.missing
    assert out.provenance == [ProvenanceRecord("lqm-threshold-stub", "lqm-threshold-stub@0.1", NOW, NOW)]


@pytest.mark.parametrize("val,expected", [(5.1, 1.0), (4.9, 0.0), (5.0, 0.0)])
def test_threshold_behaviour(val, expected):
    assert lqm(threshold=5.0).predict([env(value=val)]).value == expected


def test_stale_missing_and_non_numeric_ignored():
    inputs = [env(value=100.0, stale=True), env(value=100.0, missing=True),
              env(value=True), env(value="high"), env(value=1.0)]
    assert lqm(threshold=5.0).predict(inputs).value == 0.0


def test_provenance_is_the_model_then_one_record_per_input_in_order():
    out = lqm().predict([env("a", 1.0, T2), env("b", 1.0, T1)])
    assert out.entity_id == "a"
    assert out.provenance == [
        ProvenanceRecord("lqm-threshold-stub", "lqm-threshold-stub@0.1", NOW, NOW),
        ProvenanceRecord("tsdb", "a", T2, NOW),
        ProvenanceRecord("tsdb", "b", T1, NOW),
    ]


def test_inputs_without_usable_values_still_leave_a_record_each():
    out = lqm().predict([env("a", None, None, missing=True)])
    assert out.provenance[1] == ProvenanceRecord("tsdb", "a", None, NOW)


def test_model_clock_is_read_once_per_prediction():
    ticks = iter([NOW, NOW.replace(hour=14), NOW.replace(hour=15)])
    stub = ThresholdLqmStub(registry(), now=lambda: next(ticks))
    out = stub.predict([env("a", 1.0, T1)])
    assert out.observed_at == NOW
    assert {r.retrieved_at for r in out.provenance} == {NOW}


def test_provenance_serialises_as_a_list_of_four_key_objects():
    import json
    d = json.loads(json.dumps(lqm().predict([env("a", 1.0, T1)]).to_dict()))
    assert [sorted(r) for r in d["provenance"]] == [
        ["observed_at", "reference", "retrieved_at", "source_system"]] * 2


def _tracked_python_files():
    import subprocess
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=root,
                         capture_output=True, text=True, check=True).stdout.split()
    return root, [f for f in out if not f.startswith(("venv/", "docs/"))]


def test_no_module_assigns_a_dict_to_provenance():
    import ast
    root, files = _tracked_python_files()
    assert files
    bad = []
    for f in files:
        for n in ast.walk(ast.parse((root / f).read_text())):
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(n.value, ast.Dict):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                if any((isinstance(t, ast.Name) and t.id == "provenance")
                       or (isinstance(t, ast.Attribute) and t.attr == "provenance") for t in targets):
                    bad.append(f"{f}:{n.lineno}")
            if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == "ContextEnvelope":
                bad += [f"{f}:{n.lineno}" for k in n.keywords
                        if k.arg == "provenance" and isinstance(k.value, ast.Dict)]
    assert bad == []


def ctx(*kept, dropped=0):
    return PrunedContext(kept=list(kept), dropped_count=dropped)


def router():
    return RouterStub(registry())


@pytest.mark.parametrize("bad", [env(stale=True), env(missing=True, value=None), env(conflicts=["x"])])
def test_router_investigates_on_quality_issue(bad):
    assert router().route(ctx(env(), bad)) == "investigate"


def test_router_ignores_healthy_even_if_dropped():
    out = router().route(ctx(env(), dropped=3))
    assert out == "ignore"
    assert type(out) is str


def test_router_empty_context_ignore():
    assert router().route(ctx()) in {"investigate", "ignore"}


def test_investigation_workers_does_not_import_action_modules():
    import ast

    forbidden = ("governance_gate", "action_lifecycle")
    root = Path(investigation_workers.__file__).parent
    files = list(root.rglob("*.py"))
    assert files
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name.split(".")[0] not in forbidden, (path, name)
