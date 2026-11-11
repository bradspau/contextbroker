"""read_in_order: ordered results, a bound on concurrency, first failure in order."""
import threading
import time

import pytest

from broker_engine.telemetry_reads import read_in_order


@pytest.mark.parametrize("workers", [1, 2, 3, 8])
@pytest.mark.parametrize("n", [0, 1, 2, 5, 9])
def test_results_are_in_item_order(workers, n):
    items = list(range(n))

    def slow_for_early(x):
        time.sleep(0.01 * (n - x))  # early items finish last
        return x * 10

    assert read_in_order(slow_for_early, items, workers) == [x * 10 for x in items]


def test_one_item_or_one_worker_runs_on_the_calling_thread():
    main = threading.get_ident()
    assert read_in_order(lambda _: threading.get_ident(), [1], 8) == [main]
    assert read_in_order(lambda _: threading.get_ident(), [1, 2, 3], 1) == [main] * 3


def test_concurrency_is_bounded_by_workers():
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def fn(x):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return x

    assert read_in_order(fn, [1, 2, 3, 4], 2) == [1, 2, 3, 4]
    assert state["peak"] == 2


def test_first_failing_item_in_order_is_raised():
    class A(Exception):
        pass

    class B(Exception):
        pass

    def fn(x):
        if x == 2:
            time.sleep(0.1)
            raise A()
        if x == 3:
            raise B()
        return x

    with pytest.raises(A):
        read_in_order(fn, [1, 2, 3, 4], 4)


@pytest.mark.parametrize("workers", [0, 9, -1, True, "2", 2.0])
def test_workers_outside_one_to_eight_are_refused(workers):
    with pytest.raises(ValueError):
        read_in_order(lambda x: x, [1, 2], workers)


def test_default_workers_is_used_when_none_is_given():
    assert read_in_order(lambda x: x + 1, [1, 2, 3]) == [2, 3, 4]
