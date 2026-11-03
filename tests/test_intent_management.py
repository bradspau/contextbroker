import threading

import pytest

import intent_management.store as store_mod
from intent_management.store import IntentStore
from outcome_assurance.evaluate import Outcome


def test_fresh_store_has_no_outcome_and_zero_tracked():
    store = IntentStore()
    assert store.last_outcome("I1") is None
    assert store.tracked_count() == 0


def test_fulfilment_update_then_last_outcome():
    store = IntentStore()
    store.fulfilment_update("I1", Outcome.FAILURE)
    assert store.last_outcome("I1") == Outcome.FAILURE
    assert store.last_outcome("I2") is None
    assert store.tracked_count() == 1


def test_second_update_replaces_without_increasing_count():
    store = IntentStore()
    store.fulfilment_update("I1", Outcome.FAILURE)
    store.fulfilment_update("I1", Outcome.SUCCESS)
    assert store.last_outcome("I1") == Outcome.SUCCESS
    assert store.tracked_count() == 1


def test_concurrent_updates_leave_a_consistent_count():
    store = IntentStore()
    barrier = threading.Barrier(8)

    def work(n):
        barrier.wait()
        for i in range(50):
            store.fulfilment_update(f"I{n}-{i}", Outcome.SUCCESS)
            store.fulfilment_update("shared", Outcome.INCONCLUSIVE)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.tracked_count() == 8 * 50 + 1
    assert store.last_outcome("shared") == Outcome.INCONCLUSIVE


def test_constructor_rejects_a_resolver_argument():
    with pytest.raises(TypeError):
        IntentStore(lambda i: {})


def test_store_keeps_only_outcomes():
    assert not hasattr(store_mod, "Intent")
    assert not hasattr(store_mod, "_REQUIRED")
    assert not hasattr(IntentStore, "get")
    assert not hasattr(IntentStore, "with_resolver")


def test_tmf921_store_is_an_outcome_store():
    from intent_management import tmf921_source
    assert isinstance(tmf921_source.TMF921_STORE, IntentStore)
