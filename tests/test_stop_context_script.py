"""stop_context.sh stops only the telemetry simulator named by a pid file.

Exit-code contract: 0 after the simulator was signalled and the pid file removed; 1 with one
fixed line on stderr for a missing or unreadable pid file, a value that is not digits only, or
a process whose command line does not name telemetry_simulator. The script never touches the
compose stack.
"""
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ps") is None,
                                reason="ps is not installed; the script needs it")

SCRIPT = Path(__file__).resolve().parent.parent / "stop_context.sh"


def _run(cwd, *args):
    return subprocess.run(["sh", str(SCRIPT), *args], cwd=cwd, capture_output=True,
                          text=True, timeout=20)


def _spawn(*extra):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *extra])


def _alive(proc):
    return proc.poll() is None


def _wait_exit(proc, seconds=5):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return False


def _wait_named(proc, word):
    """Wait until ps shows the new process's command line (it is set at exec)."""
    deadline = time.time() + 5
    while time.time() < deadline:
        out = subprocess.run(["ps", "-ww", "-p", str(proc.pid), "-o", "args="], capture_output=True,
                             text=True).stdout
        if word in out:
            return
        time.sleep(0.05)
    raise AssertionError("process command line never showed " + word)


def test_it_signals_the_simulator_and_removes_the_pid_file(tmp_path):
    proc = _spawn("telemetry_simulator")
    try:
        _wait_named(proc, "telemetry_simulator")
        (tmp_path / "telemetry.pid").write_text(f"{proc.pid}\n")
        result = _run(tmp_path)
        assert result.returncode == 0, result.stderr
        assert _wait_exit(proc)
        assert proc.returncode == -15
        assert not (tmp_path / "telemetry.pid").exists()
    finally:
        if _alive(proc):
            proc.kill()
        proc.wait()


def test_a_process_that_is_not_the_simulator_is_left_alone(tmp_path):
    proc = _spawn()
    try:
        _wait_named(proc, "time.sleep")
        (tmp_path / "telemetry.pid").write_text(f"{proc.pid}\n")
        result = _run(tmp_path)
        assert result.returncode == 1
        assert result.stdout == ""
        assert len(result.stderr.strip().splitlines()) == 1
        time.sleep(0.3)
        assert _alive(proc)
    finally:
        proc.kill()
        proc.wait()


def test_no_pid_file_is_one_fixed_line(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 1
    assert result.stdout == ""
    assert len(result.stderr.strip().splitlines()) == 1


@pytest.mark.parametrize("content", ["", "\n", "abc", "12 34", "-5", "1;id", "$(id)", "0x10"])
def test_a_malformed_pid_file_signals_nothing(tmp_path, content):
    (tmp_path / "telemetry.pid").write_text(content)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert result.stdout == ""
    assert len(result.stderr.strip().splitlines()) == 1
    assert content.strip() == "" or content.strip() not in result.stderr
    assert (tmp_path / "telemetry.pid").exists()


def test_an_explicit_path_is_used_instead_of_the_default(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (tmp_path / "telemetry.pid").write_text("not a pid")
    proc = _spawn("telemetry_simulator")
    try:
        _wait_named(proc, "telemetry_simulator")
        pidfile = other / "sim.pid"
        pidfile.write_text(str(proc.pid))
        result = _run(tmp_path, str(pidfile))
        assert result.returncode == 0, result.stderr
        assert _wait_exit(proc)
        assert not pidfile.exists()
        assert (tmp_path / "telemetry.pid").exists()
    finally:
        if _alive(proc):
            proc.kill()
        proc.wait()


# --- static checks on the file -------------------------------------------------------------

def _code_lines():
    out = []
    for raw in SCRIPT.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def test_the_script_starts_with_a_posix_shell_line():
    assert SCRIPT.read_text().splitlines()[0] == "#!/bin/sh"


def test_the_script_has_no_container_command():
    for line in _code_lines():
        assert not re.search(r"\b(docker|compose|podman)\b", line), line


def test_every_expansion_sits_inside_double_quotes():
    for line in _code_lines():
        in_quote = False
        i = 0
        while i < len(line):
            c = line[i]
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_quote = not in_quote
            elif c == "$" and not in_quote:
                # an unquoted expansion is allowed only as the right side of an assignment
                # (no word splitting there) or inside a case pattern's subject
                before = line[:i]
                assert re.search(r"(^|\s)\w+=\$?\(?$", before) or before.rstrip().endswith(
                    "case"), line
            i += 1


def test_kill_is_given_one_id_and_nothing_else_is_killed():
    kills = [l for l in _code_lines() if re.search(r"\bkill\b", l)]
    assert len(kills) == 1 and kills[0].startswith('kill "$pid"'), kills
    assert not re.search(r"\b(pkill|killall|xargs)\b", SCRIPT.read_text())
