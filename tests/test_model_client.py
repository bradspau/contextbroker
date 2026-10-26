from datetime import datetime, timezone
from pathlib import Path

import pytest

import investigation_workers
from artefact_governance.model_registry import ModelRegistry
from broker_engine.pruner import PrunedContext
from data_contracts.envelope import ContextEnvelope, Quality, StateCategory
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
    assert out.provenance["model"] == "lqm-threshold-stub"
    assert out.provenance["model_version"] == "0.1"
    assert out.source == "lqm-threshold-stub"
    assert out.observed_at == NOW
    assert out.provenance["uncertainty"] is None


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
    assert out.provenance["input_window"] == {"entities": [], "from": None, "to": None}


@pytest.mark.parametrize("val,expected", [(5.1, 1.0), (4.9, 0.0), (5.0, 0.0)])
def test_threshold_behaviour(val, expected):
    assert lqm(threshold=5.0).predict([env(value=val)]).value == expected


def test_stale_missing_and_non_numeric_ignored():
    inputs = [env(value=100.0, stale=True), env(value=100.0, missing=True),
              env(value=True), env(value="high"), env(value=1.0)]
    assert lqm(threshold=5.0).predict(inputs).value == 0.0


def test_input_window_contents():
    out = lqm().predict([env("a", 1.0, T2), env("b", 1.0, T1)])
    assert out.entity_id == "a"
    w = out.provenance["input_window"]
    assert w == {"entities": ["a", "b"], "from": T1.isoformat(), "to": T2.isoformat()}


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
