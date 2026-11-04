from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from broker_engine.binding_resolver import resolve_binding
from broker_engine.validation import ClientInputError

URI = "http://broadband-forum.org/Intent#LatencyMetric"
URI2 = "http://broadband-forum.org/Intent#JitterMetric"
POST = "broker_engine.binding_resolver.requests.post"
NS = "http://example.org/network-ontology#"
IF1 = NS + "if-olt1-pon0-1"
IF2 = NS + "if-olt1-pon0-2"
START = "2026-11-01T10:00:00+00:00"
LATER = "2026-11-03T10:00:00+00:00"
V = datetime(2026, 11, 2, tzinfo=timezone.utc)


def _cells(**kw):
    return {k: {"value": v} for k, v in kw.items() if v is not None}


def bare(metric=URI):
    """A binding node with no fluent: Fuseki leaves every unbound variable out."""
    return _cells(metric=metric)


def goal(metric=URI, f=NS + "goal-1", val="100.0", vs=START, ve=None, ra=START, sa=None):
    return _cells(metric=metric, kind="goal", f=f, val=val, vs=vs, ve=ve, ra=ra, sa=sa)


def place(metric=URI, f=NS + "place-1", val=IF1, device="olt1", iface="pon0/1", vs=START,
          ve=None, ra=START, sa=None):
    return _cells(metric=metric, kind="placement", f=f, val=val, vs=vs, ve=ve, ra=ra, sa=sa,
                  device=device, iface=iface)


def full(metric=URI, n=1, pin="100.0", iface="pon0/1"):
    """The goal row and the placement row of one metric."""
    return [goal(metric, NS + f"goal-{n}", pin), place(metric, NS + f"place-{n}", NS + f"if-olt1-{n}", "olt1", iface)]


def resp(rows):
    r = Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = {"results": {"bindings": rows}}
    return r


def get(rows, **kw):
    with patch(POST, return_value=resp(rows)):
        return resolve_binding("hsi-1", valid_at=kw.pop("valid_at", V), **kw)


def test_sends_the_credential_when_set(monkeypatch):
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "testpassword0")
    with patch(POST, return_value=resp([])) as post:
        resolve_binding("hsi-1")
    assert post.call_args.kwargs["auth"] == ("admin", "testpassword0")
    assert post.call_args.kwargs["headers"] == {"Accept": "application/sparql-results+json"}


def test_sends_no_credential_when_unset(monkeypatch):
    monkeypatch.delenv("FUSEKI_ADMIN_PASSWORD", raising=False)
    with patch(POST, return_value=resp([])) as post:
        resolve_binding("hsi-1")
    assert post.call_args.kwargs["auth"] is None


def test_fuseki_401_propagates_as_request_exception(monkeypatch):
    import requests
    monkeypatch.setenv("FUSEKI_ADMIN_PASSWORD", "testpassword0")
    refused = Mock()
    refused.raise_for_status.side_effect = requests.HTTPError("401 Client Error: Unauthorized")
    with patch(POST, return_value=refused):
        with pytest.raises(requests.RequestException) as exc:
            resolve_binding("hsi-1")
    assert "testpassword0" not in str(exc.value)


def test_returns_none_when_no_rows():
    with patch(POST, return_value=resp([])):
        assert resolve_binding("hsi-1") is None


def test_builds_sources_and_bounds_from_rows():
    rows = [*full(), *full(URI2, 2, "10", "pon0/2")]
    with patch(POST, return_value=resp(rows)) as p:
        b = resolve_binding("hsi-1", valid_at=V)
    assert b.intent_id == "hsi-1"
    assert b.sources == {URI: ("olt1", "pon0/1"), URI2: ("olt1", "pon0/2")}
    assert b.bounds == {URI: 100.0, URI2: 10.0}
    assert '"hsi-1"' in p.call_args.kwargs["data"]["query"]


def test_the_query_starts_from_the_binding_nodes_and_reads_fluents_in_one_optional():
    with patch(POST, return_value=resp([])) as p:
        resolve_binding("hsi-1")
    query = p.call_args.kwargs["data"]["query"]
    assert "OPTIONAL" in query and "UNION" in query
    for name in (":hasGoal", ":hasPlacement", ":validStart", ":validEnd", ":recordedAt",
                 ":supersededAt", ":pinnedBound", ":onInterface"):
        assert name in query


def test_identical_duplicate_rows_collapse():
    assert get([*full(), *full()]).bounds == {URI: 100.0}


def test_two_goals_in_force_at_once_are_invalid():
    rows = [*full(), goal(f=NS + "goal-2", val="5")]
    with pytest.raises(ValueError, match="duplicate binding for metric"):
        get(rows)


def test_two_placements_in_force_at_once_are_invalid():
    rows = [*full(), place(f=NS + "place-2", val=IF2, iface="pon0/2")]
    with pytest.raises(ValueError, match="duplicate binding for metric"):
        get(rows)


def test_one_interface_node_with_two_names_is_invalid():
    rows = [*full(), place(f=NS + "place-1", val=IF1, iface="pon0/2")]
    with pytest.raises(ValueError, match="duplicate binding for metric"):
        get(rows)


@pytest.mark.parametrize("pin", ["NaN", "INF", "-INF", "abc", ""])
def test_non_finite_pin_is_invalid(pin):
    with pytest.raises(ValueError, match="invalid pinned bound"):
        get([goal(val=pin), place()])


@pytest.mark.parametrize("bad", [dict(metric="not a uri"), dict(f="not a uri")])
def test_bad_uri_in_a_goal_row_is_invalid(bad):
    with pytest.raises(ValueError):
        get([goal(**bad), place()])


@pytest.mark.parametrize("bad", [dict(device="r1;drop"), dict(iface="a b"),
                                 dict(val="not a uri"), dict(f="not a uri"),
                                 dict(metric="not a uri")])
def test_bad_value_in_a_placement_row_is_invalid(bad):
    with pytest.raises(ValueError):
        get([goal(), place(**bad)])


def test_a_placement_row_without_a_device_is_malformed():
    row = place()
    del row["device"]
    with pytest.raises(ValueError, match="malformed binding result"):
        get([goal(), row])


@pytest.mark.parametrize("bad", ["2026-11-01T10:00:00", "yesterday", "2026-11-01"])
@pytest.mark.parametrize("column", ["vs", "ve", "ra", "sa"])
def test_a_timestamp_without_a_zone_is_malformed(column, bad):
    with pytest.raises(ValueError, match="malformed binding result"):
        get([goal(**{column: bad}), place()])


@pytest.mark.parametrize("stamp", ["2026-11-01T10:00:00Z", "2026-11-01T11:00:00+01:00",
                                   "2026-11-01T10:00:00.000Z"])
def test_zone_forms_are_compared_as_instants(stamp):
    assert get([goal(vs=stamp, ra=stamp), place(vs=stamp, ra=stamp)]).bounds == {URI: 100.0}


def test_a_row_with_a_fluent_but_no_times_is_malformed():
    row = goal()
    del row["vs"]
    with pytest.raises(ValueError, match="malformed binding result"):
        get([row, place()])


def test_an_unknown_kind_is_malformed():
    row = goal()
    row["kind"] = {"value": "other"}
    with pytest.raises(ValueError, match="malformed binding result"):
        get([row, place()])


def test_malformed_result_is_invalid():
    r = Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = {"oops": 1}
    with patch(POST, return_value=r):
        with pytest.raises(ValueError):
            resolve_binding("hsi-1")


def test_a_row_that_is_not_an_object_is_malformed():
    with pytest.raises(ValueError, match="malformed binding result"):
        get(["row"])


@pytest.mark.parametrize("iid", ['x" } ; DROP', "a b", "", "x\n", None])
def test_unsafe_intent_id_rejected_before_any_request(iid):
    with patch(POST) as p:
        with pytest.raises(ClientInputError):
            resolve_binding(iid)
    p.assert_not_called()


# ---- selection by valid time and knowledge time -------------------------------------------


def replaced():
    """Goal 100 until LATER, then 150; one placement throughout."""
    return [goal(f=NS + "goal-1", val="100.0", ve=LATER),
            goal(f=NS + "goal-2", val="150.0", vs=LATER, ra=LATER),
            place()]


def test_the_goal_in_force_at_the_valid_time_is_returned():
    assert get(replaced(), valid_at=V).bounds == {URI: 100.0}
    later = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    assert get(replaced(), valid_at=later).bounds == {URI: 150.0}
    assert get(replaced(), valid_at=later - timedelta(milliseconds=1)).bounds == {URI: 100.0}


def test_no_valid_time_means_now():
    assert get(replaced(), valid_at=None).bounds == {URI: 150.0}


def test_known_at_none_is_latest_and_skips_superseded_fluents():
    rows = [goal(), place(f=NS + "place-1", sa=LATER, val=IF1),
            place(f=NS + "place-2", val=IF2, iface="pon0/2", ra=LATER)]
    assert get(rows, valid_at=datetime(2026, 11, 4, tzinfo=timezone.utc)).sources == {
        URI: ("olt1", "pon0/2")}


def test_known_at_selects_the_record_the_broker_held_then():
    rows = [goal(), place(f=NS + "place-1", sa=LATER, val=IF1),
            place(f=NS + "place-2", val=IF2, iface="pon0/2", ra=LATER)]
    before = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
    at = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    assert get(rows, valid_at=V, known_at=before).sources == {URI: ("olt1", "pon0/1")}
    assert get(rows, valid_at=V, known_at=at).sources == {URI: ("olt1", "pon0/2")}


@pytest.mark.parametrize("kw", [dict(valid_at=datetime(2026, 11, 1)),
                                dict(known_at=datetime(2026, 11, 1))])
def test_a_naive_request_time_is_refused_before_any_request(kw):
    with patch(POST) as p:
        with pytest.raises(ValueError):
            resolve_binding("hsi-1", **kw)
    p.assert_not_called()


def test_a_valid_time_before_the_first_fluent_gives_both_gaps():
    before = datetime(2026, 10, 1, tzinfo=timezone.utc)
    b = get([*full()], valid_at=before)
    assert b.gaps == ((URI, "goal"), (URI, "placement"))
    assert b.bounds == {} and b.sources == {}
    assert b.goal_intervals == {} and b.placement_intervals == {}


def test_a_metric_with_a_goal_and_no_placement_has_a_placement_gap():
    b = get([goal()])
    assert b.gaps == ((URI, "placement"),)
    assert b.bounds == {} and b.sources == {}


def test_a_metric_with_a_placement_and_no_goal_has_a_goal_gap():
    assert get([place()]).gaps == ((URI, "goal"),)


def test_a_binding_node_with_no_fluent_gives_both_gaps():
    b = get([bare()])
    assert b is not None
    assert b.gaps == ((URI, "goal"), (URI, "placement"))


def test_gaps_are_sorted_by_metric_then_kind():
    b = get([bare(URI), bare(URI2)])
    assert b.gaps == ((URI2, "goal"), (URI2, "placement"), (URI, "goal"), (URI, "placement"))


def test_only_the_uncovered_metric_is_a_gap():
    b = get([*full(), bare(URI2)])
    assert b.gaps == ((URI2, "goal"), (URI2, "placement"))
    assert b.bounds == {URI: 100.0}
    assert b.sources == {URI: ("olt1", "pon0/1")}


def test_a_superseded_only_placement_is_a_gap_when_known_at_is_latest():
    rows = [goal(), place(sa=LATER)]
    b = get(rows, valid_at=V)
    assert b.gaps == ((URI, "placement"),)
    # ...but it is the record the broker held before it was superseded.
    held = get(rows, valid_at=V, known_at=datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc))
    assert held.gaps == ()


def test_a_closed_goal_with_no_successor_is_a_gap_after_its_end():
    rows = [goal(ve=LATER), place()]
    later = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    assert get(rows, valid_at=later).gaps == ((URI, "goal"),)
    assert get(rows, valid_at=later - timedelta(milliseconds=1)).gaps == ()


def test_the_intervals_of_the_selected_fluents_are_returned():
    b = get(replaced(), valid_at=V)
    start = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    later = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    assert b.goal_intervals == {URI: (start, later)}
    assert b.placement_intervals == {URI: (start, None)}
    assert get(replaced(), valid_at=later).goal_intervals == {URI: (later, None)}


def test_a_gap_does_not_raise():
    assert get([bare()]).gaps


def test_the_binding_still_constructs_from_three_arguments():
    from data_contracts.intent_bindings import IntentBinding
    b = IntentBinding("i", {}, {})
    assert (b.gaps, b.goal_intervals, b.placement_intervals) == ((), {}, {})
    assert IntentBinding("i", {}, {}) == IntentBinding("i", {}, {})
