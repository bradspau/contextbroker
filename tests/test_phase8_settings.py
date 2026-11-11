"""Parsing and start-up refusal for the Phase 8 settings (BROKER_TELEMETRY_WORKERS)."""
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from broker_engine import telemetry_reads
from broker_engine.validation import (
    DEFAULT_TELEMETRY_WORKERS,
    DEFAULT_TSDB_SERIES_LIMIT,
    MAX_TELEMETRY_WORKERS,
    MAX_TSDB_SERIES_LIMIT,
    TELEMETRY_WORKERS_ENV,
    TSDB_SERIES_LIMIT_ENV,
    telemetry_workers,
    tsdb_series_limit,
)

MESSAGE = "BROKER_TELEMETRY_WORKERS must be a whole number from 1 to 8"
ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = ROOT / "docker-compose.yml"
ENV_EXAMPLE_PATH = ROOT / ".env.example"
PHASE8_ITEMS = [
    "BROKER_TELEMETRY_WORKERS=${BROKER_TELEMETRY_WORKERS:-8}",
    "BROKER_TSDB_SERIES_LIMIT=${BROKER_TSDB_SERIES_LIMIT:-500}",
]
PHASE8_KEYS = (TELEMETRY_WORKERS_ENV, TSDB_SERIES_LIMIT_ENV)


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


# ---------- BROKER_TSDB_SERIES_LIMIT ----------
SERIES_MESSAGE = "BROKER_TSDB_SERIES_LIMIT must be a whole number from 1 to 100000"


def test_series_limit_names_and_defaults():
    assert TSDB_SERIES_LIMIT_ENV == "BROKER_TSDB_SERIES_LIMIT"
    assert DEFAULT_TSDB_SERIES_LIMIT == 500 and MAX_TSDB_SERIES_LIMIT == 100000


@pytest.mark.parametrize("environ", [{}, {TSDB_SERIES_LIMIT_ENV: ""}])
def test_series_limit_unset_or_blank_gives_the_default(environ):
    assert tsdb_series_limit(environ) == 500


@pytest.mark.parametrize("raw", ["1", "500", "100000"])
def test_series_limit_whole_numbers_in_range_are_accepted(raw):
    assert tsdb_series_limit({TSDB_SERIES_LIMIT_ENV: raw}) == int(raw)


@pytest.mark.parametrize("raw", ["0", "100001", "abc", " 5", "-1", "1e3", "canary99", "5\n"])
def test_series_limit_a_bad_value_is_refused_naming_the_variable_and_never_the_value(raw):
    with pytest.raises(ValueError) as info:
        tsdb_series_limit({TSDB_SERIES_LIMIT_ENV: raw})
    assert str(info.value) == SERIES_MESSAGE
    if raw.strip() not in SERIES_MESSAGE:  # "0" and "1" are in the range text itself
        assert raw.strip() not in str(info.value)


def _import_main_with(name, value):
    env = dict(os.environ)
    env.pop(name, None)
    if value is not None:
        env[name] = value
    return subprocess.run([sys.executable, "-c", "import broker_engine.main"], env=env,
                          cwd=ROOT, capture_output=True, text=True, timeout=60)


def test_a_bad_series_limit_stops_the_api_at_start_naming_only_the_variable():
    bad = _import_main_with(TSDB_SERIES_LIMIT_ENV, "canary99")
    assert bad.returncode != 0
    assert TSDB_SERIES_LIMIT_ENV in bad.stderr
    assert "canary99" not in bad.stderr


@pytest.mark.parametrize("value", [None, "1000"])
def test_an_unset_or_good_series_limit_lets_the_api_start(value):
    ok = _import_main_with(TSDB_SERIES_LIMIT_ENV, value)
    assert ok.returncode == 0, ok.stderr


# ---------- compose and .env.example ----------
def test_compose_gives_the_api_each_phase8_setting_once_and_no_other_service_names_them():
    services = yaml.safe_load(COMPOSE_PATH.read_text())["services"]
    api_env = services["contextbroker-api"]["environment"]
    for item in PHASE8_ITEMS:
        assert api_env.count(item) == 1, item
    for name, svc in services.items():
        if name == "contextbroker-api":
            continue
        for key in PHASE8_KEYS:
            assert key not in repr(svc), (name, key)


def test_compose_defaults_equal_the_code_defaults():
    api_env = yaml.safe_load(COMPOSE_PATH.read_text())["services"]["contextbroker-api"]["environment"]
    pattern = re.compile(r"^(BROKER_(?:TELEMETRY_WORKERS|TSDB_SERIES_LIMIT))=\$\{\1:-([^}]*)\}$")
    defaults = {m.group(1): m.group(2) for m in map(pattern.match, api_env) if m}
    assert set(defaults) == set(PHASE8_KEYS)
    assert telemetry_workers({TELEMETRY_WORKERS_ENV: defaults[TELEMETRY_WORKERS_ENV]}) \
        == telemetry_workers({})
    assert tsdb_series_limit({TSDB_SERIES_LIMIT_ENV: defaults[TSDB_SERIES_LIMIT_ENV]}) \
        == tsdb_series_limit({})


def test_env_example_documents_each_phase8_setting_with_a_range_line_and_no_live_assignment():
    lines = ENV_EXAMPLE_PATH.read_text().splitlines()
    block_start = next(i for i, line in enumerate(lines) if line.startswith("# Broker state."))
    block_end = lines.index("# TMF921 server (compose profile tmf921)")
    expected = {TELEMETRY_WORKERS_ENV: "8", TSDB_SERIES_LIMIT_ENV: "500"}
    for key, default in expected.items():
        assert not any(line.startswith(key + "=") for line in lines), key
        indexes = [i for i, line in enumerate(lines) if line.startswith(f"# {key}=")]
        assert len(indexes) == 1, key
        i = indexes[0]
        assert lines[i] == f"# {key}={default}", key
        assert lines[i - 1].startswith("# ") and not re.match(r"# BROKER_[A-Z_]+=", lines[i - 1]), key
        assert " to " in lines[i - 1], key
        assert block_start < i < block_end, key
