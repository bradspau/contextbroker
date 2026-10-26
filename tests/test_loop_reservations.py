from datetime import datetime, timedelta, timezone
import pytest
from loop_coordination.reservations import ReservationBook

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_reserve_and_holder():
    b = ReservationBook()
    assert b.reserve("a", "s1", 60, T0) is True
    assert b.holder("s1", T0) == "a"
    assert b.holder("other", T0) is None


def test_conflict_denied():
    b = ReservationBook()
    b.reserve("a", "s1", 60, T0)
    assert b.reserve("b", "s1", 60, T0 + timedelta(seconds=10)) is False
    assert b.holder("s1", T0) == "a"


def test_expired_reusable():
    b = ReservationBook()
    b.reserve("a", "s1", 60, T0)
    later = T0 + timedelta(seconds=61)
    assert b.holder("s1", later) is None
    assert b.reserve("b", "s1", 60, later) is True
    assert b.holder("s1", later) == "b"


def test_same_loop_refreshes():
    b = ReservationBook()
    b.reserve("a", "s1", 60, T0)
    assert b.reserve("a", "s1", 60, T0 + timedelta(seconds=50)) is True
    assert b.holder("s1", T0 + timedelta(seconds=100)) == "a"


def test_release_only_by_holder():
    b = ReservationBook()
    b.reserve("a", "s1", 60, T0)
    b.release("b", "s1")
    assert b.holder("s1", T0) == "a"
    b.release("a", "s1")
    assert b.holder("s1", T0) is None


@pytest.mark.parametrize("ttl", [0, -5])
def test_bad_ttl(ttl):
    with pytest.raises(ValueError):
        ReservationBook().reserve("a", "s", ttl, T0)
