"""Parsing and start-up refusal for the Phase 8 settings (BROKER_TELEMETRY_WORKERS)."""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from broker_engine import telemetry_reads
from broker_engine.validation import (
    DEFAULT_TELEMETRY_WORKERS,
    MAX_TELEMETRY_WORKERS,
    TELEMETRY_WORKERS_ENV,
    telemetry_workers,
)

MESSAGE = "BROKER_TELEMETRY_WORKERS must be a whole number from 1 to 8"
ROOT = Path(__file__).resolve().parent.parent


def test_names_and_defaults():
    assert TELEMETRY_WORKERS_ENV == "BROKER_TELEMETRY_WORKERS"
    assert DEFAULT_TELEMETRY_WORKERS == 8 and MAX_TELEMETRY_WORKERS == 8


@pytest.mark.parametrize("environ", [{}, {TELEMETRY_WORKERS_ENV: ""}])
def test_unset_or_blank_gives_the_default(environ):
    assert telemetry_workers(environ) == 8


@pytest.mark.parametrize("raw", ["1", "2", "8"])
def test_whole_numbers_from_one_to_eight_are_accepted(raw):
    assert telemetry_workers({TELEMETRY_WORKERS_ENV: raw}) == int(raw)


@pytest.mark.parametrize("raw", ["0", "9", "abc", " 5", "-1", "1e3", "canary99", "5\n", "08x"])
def test_a_bad_value_is_refused_naming_the_variable_and_never_the_value(raw):
    with pytest.raises(ValueError) as info:
        telemetry_workers({TELEMETRY_WORKERS_ENV: raw})
    assert str(info.value) == MESSAGE
    assert raw.strip() not in str(info.value)


def _import_main(workers):
    env = dict(os.environ)
    env.pop(TELEMETRY_WORKERS_ENV, None)
    if workers is not None:
        env[TELEMETRY_WORKERS_ENV] = workers
    return subprocess.run([sys.executable, "-c", "import broker_engine.main"], env=env,
                          cwd=ROOT, capture_output=True, text=True, timeout=60)


def test_a_bad_value_stops_the_api_at_start_naming_only_the_variable():
    bad = _import_main("canary99")
    assert bad.returncode != 0
    assert TELEMETRY_WORKERS_ENV in bad.stderr
    assert "canary99" not in bad.stderr


@pytest.mark.parametrize("workers", [None, "4"])
def test_unset_or_good_value_lets_the_api_start(workers):
    ok = _import_main(workers)
    assert ok.returncode == 0, ok.stderr


def test_one_worker_reads_every_item_on_the_calling_thread(monkeypatch):
    monkeypatch.setenv(TELEMETRY_WORKERS_ENV, "1")
    main = threading.get_ident()
    assert telemetry_reads.read_in_order(lambda _: threading.get_ident(), [1, 2, 3, 4]) \
        == [main] * 4


def test_three_workers_never_run_more_than_three_at_once(monkeypatch):
    monkeypatch.setenv(TELEMETRY_WORKERS_ENV, "3")
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

    assert telemetry_reads.read_in_order(fn, list(range(7))) == list(range(7))
    assert state["peak"] == 3


def test_the_setting_is_read_on_each_call(monkeypatch):
    main = threading.get_ident()
    monkeypatch.setenv(TELEMETRY_WORKERS_ENV, "1")
    assert telemetry_reads.read_in_order(lambda _: threading.get_ident(), [1, 2]) == [main] * 2
    monkeypatch.setenv(TELEMETRY_WORKERS_ENV, "2")
    barrier = threading.Barrier(2, timeout=5)

    def meet(_):
        barrier.wait()  # only two threads at once can pass
        return threading.get_ident()

    assert main not in telemetry_reads.read_in_order(meet, [1, 2])
