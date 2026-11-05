"""The reconciler: a recognised event kind meets a configured side read from the link fluents.

It is pure. It returns an observed envelope and conflict text built from two closed vocabularies, and
it never changes what it was given (EVT-04, D-02, D-16 to D-19, D-30).
"""
import ast
import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from data_contracts.envelope import ProvenanceRecord, StateCategory
from data_contracts.syslog_events import EVENT_KINDS
from state_reconciliation import event_reconciler
from state_reconciliation.event_reconciler import (
    CONFIGURED_LINK_IN_FORCE,
    CONFIGURED_VALUES,
    CONTRADICTIONS,
    ConfiguredSide,
    EventFact,
    ReconcileResult,
    conflict_text,
    reconcile,
)

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
EVENT_ID = "evt-" + "a" * 32
T = NOW - timedelta(seconds=60)
TEXT = "event link_down contradicts configured link_in_force"


def fact(device="Router-A", kind="link_down", at=T, event_id=EVENT_ID):
    return EventFact(event_id, device, kind, at)


def held(device="Router-A"):
    return ConfiguredSide(device, CONFIGURED_LINK_IN_FORCE)


def test_the_vocabularies_and_the_contradiction_table_are_closed():
    assert CONFIGURED_LINK_IN_FORCE == "link_in_force"
    assert CONFIGURED_VALUES == ("link_in_force",)
    assert CONTRADICTIONS == frozenset({("link_down", "link_in_force")})
    assert conflict_text("link_down", "link_in_force") == TEXT
    assert {kind for kind, _ in CONTRADICTIONS} <= set(EVENT_KINDS)


def test_a_link_down_event_against_a_link_in_force_is_an_observed_envelope_and_one_conflict():
    side = held()
    result = reconcile(fact(), side, now=NOW)
    assert isinstance(result, ReconcileResult)
    env = result.envelope
    assert env.entity_id == "Router-A" and env.value == "link_down" and env.source == "syslog"
    assert env.state is StateCategory.OBSERVED and env.observed_at == T
    assert env.provenance == [ProvenanceRecord("syslog", EVENT_ID, T, NOW)]
    assert result.conflicts == (TEXT,)
    assert env.quality.conflicts == [TEXT]
    assert not env.quality.stale and not env.quality.missing
    assert side == ConfiguredSide("Router-A", "link_in_force")  # the configured side is unchanged


@pytest.mark.parametrize("kind,configured", [
    ("link_up", held()),                                        # link_up never conflicts
    ("link_down", ConfiguredSide("Router-A", None)),            # no configured state held
    ("link_down", None),                                        # nothing read
    ("link_down", ConfiguredSide("Router-B", "link_in_force")),  # another device's configured side
    ("link_up", ConfiguredSide("Router-A", None)),
])
def test_every_other_pairing_gives_the_envelope_and_no_conflict(kind, configured):
    result = reconcile(fact(kind=kind), configured, now=NOW)
    assert result.envelope.value == kind
    assert result.conflicts == () and result.envelope.quality.conflicts == []


def test_an_unknown_event_time_gives_no_observation_time_and_a_stale_envelope():
    result = reconcile(fact(at=None), held(), now=NOW)
    assert result.envelope.observed_at is None and result.envelope.quality.stale is True
    assert result.envelope.provenance == [ProvenanceRecord("syslog", EVENT_ID, None, NOW)]
    assert result.conflicts == (TEXT,)


def test_an_old_event_is_stale_under_the_given_limit():
    old = fact(at=NOW - timedelta(seconds=301))
    assert reconcile(old, None, now=NOW).envelope.quality.stale is True
    assert reconcile(old, None, now=NOW, max_age_s=600).envelope.quality.stale is False


def test_the_envelope_is_never_configured_whatever_the_other_side_is():
    for side in (None, held(), ConfiguredSide("Router-A", None)):
        assert reconcile(fact(), side, now=NOW).envelope.state is not StateCategory.CONFIGURED


def test_a_naive_now_raises():
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        reconcile(fact(), None, now=datetime(2026, 11, 6, 12, 0))


@pytest.mark.parametrize("kind", ["link_flap", "", None, "LINK_DOWN"])
def test_a_kind_outside_the_recognised_set_raises(kind):
    with pytest.raises(ValueError):
        reconcile(fact(kind=kind), None, now=NOW)


def test_a_configured_value_outside_the_closed_set_raises():
    with pytest.raises(ValueError):
        ConfiguredSide("Router-A", "interface up")
    with pytest.raises(ValueError):
        conflict_text("link_down", "interface up")
    with pytest.raises(ValueError):
        conflict_text("link_flap", "link_in_force")


def test_the_inputs_and_the_result_are_frozen():
    side, event = held(), fact()
    with pytest.raises(FrozenInstanceError):
        side.value = None
    with pytest.raises(FrozenInstanceError):
        event.kind = "link_up"
    with pytest.raises(FrozenInstanceError):
        reconcile(event, side, now=NOW).conflicts = ()


def test_reconcile_does_not_change_its_inputs():
    event, side = fact(), held()
    before = (replace(event), replace(side))
    reconcile(event, side, now=NOW)
    assert (event, side) == before


def test_no_field_or_provenance_reference_holds_message_text():
    canary = "canary-message-text-77"
    result = reconcile(fact(), held(), now=NOW)
    body = json.dumps(result.envelope.to_dict()) + json.dumps(result.conflicts)
    assert canary not in body
    assert set(result.envelope.to_dict()["provenance"][0]) == {
        "source_system", "reference", "observed_at", "retrieved_at"}
    assert result.envelope.provenance[0].reference == EVENT_ID


def test_the_module_reads_no_clock_and_holds_no_earlier_text_comparison():
    source = Path(event_reconciler.__file__).read_text()
    assert "datetime.now" not in source and "utcnow" not in source
    for gone in ("system1-event-reconciler-stub", "unresolved device", "_norm", "SyslogEvent"):
        assert gone not in source


def test_the_reconciler_imports_only_data_contracts_and_the_standard_library():
    tree = ast.parse(Path(event_reconciler.__file__).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            roots.add(node.module.split(".")[0])
    import sys
    assert roots <= {"data_contracts"} | set(sys.stdlib_module_names), roots
