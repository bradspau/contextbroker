"""Temporal queries: as_of (point-in-time) and mode=within (worst case over a past window)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from broker_engine import assess as assess_mod
from broker_engine.assess import assess_intent
from broker_engine.temporal import temporal_observation
from broker_engine.tsdb_joiner import join_observed_metric
from broker_engine.validation import ClientInputError, max_as_of_seconds, window_seconds
from data_contracts.envelope import ContextEnvelope, Quality, StateCategory
from outcome_assurance.evaluate import Outcome, evaluate_intent

NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
AT = NOW - timedelta(minutes=10)


def _resp(payload):
    r = Mock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


# ---------- validation ----------
def test_window_seconds():
    assert window_seconds("90s", "as_of") == 90
    assert window_seconds("10m", "as_of") == 600
    assert window_seconds("2h", "as_of") == 7200
    assert window_seconds("0s", "as_of") == 0


@pytest.fixture(autouse=True)
def _default_retention(monkeypatch):
    monkeypatch.delenv("BROKER_RETENTION_PERIOD", raising=False)


@pytest.mark.parametrize("bad", ["PT10M", "10", "m", "-5m", "10m\n", "1.5h", "", "10 m",
                                 f"{max_as_of_seconds() + 1}s", "99999d"])
def test_window_seconds_rejects(bad):
    with pytest.raises(ClientInputError):
        window_seconds(bad, "as_of")


# ---------- joiner ----------
def test_join_as_of_queries_at_the_past_instant_and_judges_freshness_there():
    ts = AT.timestamp() - 30  # sample 30 s before the as_of instant
    r = _resp({"data": {"result": [{"value": [AT.timestamp(), "0"]}]}})
    r2 = _resp({"data": {"result": [{"value": [AT.timestamp(), str(ts)]}]}})
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]) as g:
        env = join_observed_metric("r1", "eth0", "input_drops", "5m", now=NOW,
                                   aggregate="last", as_of="10m")
    for call in g.call_args_list:
        assert float(call.kwargs["params"]["time"]) == AT.timestamp()
    assert g.call_args_list[0].kwargs["params"]["query"].startswith("last_over_time(")
    assert env.quality.stale is False  # fresh relative to as_of, though 10m old vs now
    assert env.observed_at == datetime.fromtimestamp(ts, timezone.utc)


def test_join_as_of_stale_relative_to_the_as_of_instant():
    ts = AT.timestamp() - 900
    r = _resp({"data": {"result": [{"value": [AT.timestamp(), "0"]}]}})
    r2 = _resp({"data": {"result": [{"value": [AT.timestamp(), str(ts)]}]}})
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]):
        env = join_observed_metric("r1", "eth0", "input_drops", "30m", now=NOW,
                                   aggregate="last", as_of="10m")
    assert env.quality.stale is True


def test_join_without_as_of_sends_no_time_param():
    r = _resp({"data": {"result": [{"value": [NOW.timestamp(), "3"]}]}})
    r2 = _resp({"data": {"result": [{"value": [NOW.timestamp(), str(NOW.timestamp())]}]}})
    with patch("broker_engine.tsdb_joiner.requests.get", side_effect=[r, r2]) as g:
        join_observed_metric("r1", "eth0", "input_drops", "5m", now=NOW)
    for call in g.call_args_list:
        assert "time" not in call.kwargs["params"]


def test_join_min_aggregate_allowed_and_bad_as_of_rejected():
    r = _resp({"data": {"result": []}})
    with patch("broker_engine.tsdb_joiner.requests.get", return_value=r) as g:
        join_observed_metric("r1", "eth0", "input_drops", "5m", now=NOW, aggregate="min")
    assert g.call_args.kwargs["params"]["query"].startswith("min_over_time(")
    with pytest.raises(ClientInputError):
        join_observed_metric("r1", "eth0", "input_drops", "5m", now=NOW, as_of="PT10M")


# ---------- assess ----------
def _env(value, eid="r1:e0", stale=False, missing=False, conflicts=()):
    return ContextEnvelope(eid, StateCategory.OBSERVED, value, "t", AT,
                           Quality(stale=stale, missing=missing, conflicts=list(conflicts)))


def _intent(op="==", target=0.0):
    return {"metric": "input_drops", "operator": op, "target_value": target}


def _join_by_agg(values):
    """values: {aggregate: value}; records the kwargs it was called with."""
    calls = []

    def join(device, iface, metric, window, **kw):
        calls.append(kw)
        v = values[kw["aggregate"]]
        return _env(v, missing=v is None)
    join.calls = calls
    return join


def _within(op, target, values):
    """Observation chosen for mode=within, and the verdict the evaluator gives it."""
    join = _join_by_agg(values)
    env = temporal_observation(join, "r1", "e0", _intent(op, target), "30m",
                               "within", "10m", NOW)
    verdict = evaluate_intent({**_intent(op, target),
                               "interfaces": [{"device": "r1", "interface_name": "e0"}]},
                              {("r1", "e0"): env})
    return join, env, verdict


def test_point_in_time_uses_last_sample_at_as_of():
    join = _join_by_agg({"last": 5.0})
    env = temporal_observation(join, "r1", "e0", _intent(), "5m", "at", "10m", NOW)
    assert join.calls == [{"aggregate": "last", "as_of": "10m", "now": NOW}]
    assert env.value == 5.0


@pytest.mark.parametrize("op,agg", [("<", "max"), ("<=", "max"), (">", "min"), (">=", "min")])
def test_within_uses_worst_case_aggregate_per_operator(op, agg):
    join, _env_out, _v = _within(op, 0.0, {"max": 1.0, "min": 1.0})
    assert [c["aggregate"] for c in join.calls] == [agg]


def test_within_equals_detects_a_breach_below_or_above_target():
    # any sample differing from 0 breaches "== 0": a low outlier counts as well as a high one
    _, up_env, up = _within("==", 0.0, {"max": 7.0, "min": 0.0})
    _, down_env, down = _within("==", 0.0, {"max": 0.0, "min": -3.0})
    _, _ok_env, ok = _within("==", 0.0, {"max": 0.0, "min": 0.0})
    assert (up.outcome, down.outcome, ok.outcome) == (
        Outcome.FAILURE, Outcome.FAILURE, Outcome.SUCCESS)
    assert up.per_interface[0]["observed"] == 7.0
    assert down.per_interface[0]["observed"] == -3.0


def test_within_equals_queries_both_extremes():
    join, _e, _v = _within("==", 0.0, {"max": 0.0, "min": 0.0})
    assert sorted(c["aggregate"] for c in join.calls) == ["max", "min"]


def test_within_equals_unusable_evidence_is_inconclusive_not_success():
    _, env, verdict = _within("==", 0.0, {"max": 0.0, "min": None})
    assert env.quality.missing is True
    assert verdict.outcome == Outcome.INCONCLUSIVE


@pytest.mark.parametrize("kw", [{"as_of": "PT10M"}, {"as_of": "10"}, {"mode": "recent"},
                                {"mode": "AT"}, {"as_of": f"{max_as_of_seconds() + 1}s"}])
def test_bad_temporal_arguments_are_client_errors(kw):
    join = _join_by_agg({"last": 0.0})
    with patch.object(assess_mod, "assess_via_tmf921") as push, \
            patch.object(assess_mod, "assess_tmf921_as_of") as asof:
        with pytest.raises(ClientInputError):
            assess_intent("i1", "5m", join=join, **kw)
    push.assert_not_called()
    asof.assert_not_called()
    assert not join.calls


@pytest.mark.parametrize("source", ["graph", "GRAPH", "", "other"])
def test_graph_source_is_a_client_error(source):
    with patch.object(assess_mod, "assess_via_tmf921") as push, \
            patch.object(assess_mod, "assess_tmf921_as_of") as asof:
        with pytest.raises(ClientInputError):
            assess_intent("i1", "5m", source=source)
        with pytest.raises(ClientInputError):
            assess_intent("i1", "5m", source=source, as_of="10m", mode="within")
    push.assert_not_called()
    asof.assert_not_called()


def test_tmf921_source_routes_temporal_arguments():
    with patch.object(assess_mod, "assess_tmf921_as_of", return_value={"verdict": "success"}) as t, \
            patch.object(assess_mod, "assess_via_tmf921") as push:
        for kw in ({"as_of": "10m"}, {"mode": "within"}, {"known_at": "1h"}):
            assert assess_intent("i1", "5m", source="tmf921", **kw) == {"verdict": "success"}
    push.assert_not_called()
    assert [c.kwargs["as_of"] for c in t.call_args_list] == ["10m", None, None]
    assert [c.kwargs["mode"] for c in t.call_args_list] == [None, "within", None]
    assert [c.kwargs["known_at"] for c in t.call_args_list] == [None, None, "1h"]


# ---------- retention (TEMP-07) ----------
@pytest.mark.parametrize("env,expected", [({}, 2592000), ({"BROKER_RETENTION_PERIOD": ""}, 2592000),
                                          ({"BROKER_RETENTION_PERIOD": "48h"}, 172800),
                                          ({"BROKER_RETENTION_PERIOD": "30d"}, 2592000),
                                          ({"BROKER_RETENTION_PERIOD": "24h"}, 86400)])
def test_max_as_of_seconds_accepts(env, expected):
    assert max_as_of_seconds(env) == expected


@pytest.mark.parametrize("bad", ["30", "30m", "12h", "1w", "30d\n", "-1d", "d", "0d", "30s", "1.5d"])
def test_max_as_of_seconds_rejects_without_echoing_the_value(bad):
    with pytest.raises(ValueError) as exc:
        max_as_of_seconds({"BROKER_RETENTION_PERIOD": bad})
    # The message is one fixed string that names the variable; it never carries the value.
    assert str(exc.value) == ("BROKER_RETENTION_PERIOD must be digits then h or d, "
                              "at least 1d, for example 30d")


def test_over_limit_reason_is_fixed_and_never_echoes_the_value():
    with pytest.raises(ClientInputError) as exc:
        window_seconds("2592001s", "as_of")
    assert str(exc.value) == "as_of is further back than the retention period"
    with pytest.raises(ClientInputError) as exc:
        window_seconds("99999d", "known_at")
    assert str(exc.value) == "known_at is further back than the retention period"
    assert window_seconds("2592000s", "as_of") == 2592000


def test_window_limit_follows_the_retention_variable(monkeypatch):
    monkeypatch.setenv("BROKER_RETENTION_PERIOD", "48h")
    assert window_seconds("47h", "as_of") == 47 * 3600
    with pytest.raises(ClientInputError):
        window_seconds("3d", "as_of")


def test_over_retention_as_of_is_refused_before_any_upstream_call():
    with patch.object(assess_mod, "assess_via_tmf921") as push, \
            patch.object(assess_mod, "assess_tmf921_as_of") as asof:
        with pytest.raises(ClientInputError):
            assess_intent("i1", "5m", as_of="31d")
    push.assert_not_called()
    asof.assert_not_called()


# ---------- plumbing ----------
def test_agent_tool_declares_and_validates_temporal_arguments():
    import json
    from agent_harness import tools
    props = next(t for t in tools.TOOLS if t["name"] == "assess_intent")["input_schema"]["properties"]
    assert props["mode"]["enum"] == ["at", "within"] and "10m" in props["as_of"]["description"]
    assert "30d" not in props["as_of"]["description"]
    assert props["known_at"]["pattern"] == props["as_of"]["pattern"]
    assert "1h" in props["known_at"]["description"] and "30d" not in props["known_at"]["description"]
    assert "as_of" in tools.SYSTEM_PROMPT and "evaluated_at" in tools.SYSTEM_PROMPT
    assert "known_at" in tools.SYSTEM_PROMPT and "goals" in tools.SYSTEM_PROMPT
    assert "goal in force then" in tools.SYSTEM_PROMPT
    assert "read now from the TMF921 intent" not in tools.SYSTEM_PROMPT
    assert "drift" not in tools.SYSTEM_PROMPT.lower()
    with patch.object(tools, "assess_intent", return_value={"verdict": "success"}) as a:
        content, is_err = tools.execute_tool(
            "assess_intent", {"intent_id": "i1", "time_window": "5m", "as_of": "10m", "mode": "at"})
    assert not is_err and a.call_args.kwargs == {"intent_id": "i1", "time_window": "5m",
                                                   "as_of": "10m", "mode": "at"}
    with patch.object(tools, "assess_intent", return_value={"verdict": "success"}) as a:
        content, is_err = tools.execute_tool(
            "assess_intent", {"intent_id": "i1", "time_window": "5m", "as_of": "2d",
                              "known_at": "36h"})
    assert not is_err and a.call_args.kwargs["known_at"] == "36h"
    content, is_err = tools.execute_tool(
        "assess_intent", {"intent_id": "i1", "time_window": "5m", "known_at": "PT1H"})
    assert is_err and "known_at" in json.loads(content)["error"]
    content, is_err = tools.execute_tool(
        "assess_intent", {"intent_id": "i1", "time_window": "5m", "known_on": "1h"})
    assert is_err
    content, is_err = tools.execute_tool(
        "assess_intent", {"intent_id": "i1", "time_window": "5m", "mode": "sometime"})
    assert is_err and "mode" in json.loads(content)["error"]
