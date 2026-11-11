"""The default install list is generated and hashed: requirements.in is the human list,
requirements.txt is what pip-compile --generate-hashes made from it, and the API image installs
it with hash checking on a base image pinned by digest."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REGENERATE = (
    "pip-compile --generate-hashes --strip-extras --output-file=requirements.txt requirements.in"
)
INSTALL_LINE = (
    "RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt"
)
HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")


def _joined(path):
    """The file's logical lines: backslash continuations joined, comments and blanks dropped."""
    logical = []
    pending = ""
    for raw in path.read_text().splitlines():
        text = raw.split("#", 1)[0].rstrip() if not pending else raw.rstrip()
        if not text.strip():
            continue
        if text.endswith("\\"):
            pending += text[:-1].strip() + " "
            continue
        logical.append((pending + text.strip()).strip())
        pending = ""
    assert pending == "", "a continuation line ends the file"
    return logical


def _lock(path):
    """name -> (version, [hashes]) for every requirement of a generated lock."""
    out = {}
    for line in _joined(path):
        head, *rest = line.split()
        match = re.fullmatch(r"([A-Za-z0-9_.\-]+)==(\S+)", head)
        assert match, f"not a name==version requirement: {line[:60]}"
        out[match.group(1).lower().replace("_", "-")] = (match.group(2), rest)
    return out


def _inputs(path):
    pins = {}
    for line in _joined(path):
        match = re.fullmatch(r"([A-Za-z0-9_.\-]+)==(\S+)", line)
        if match:
            pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    return pins


def test_every_lock_requirement_is_pinned_and_hashed_and_the_parse_is_not_empty():
    lock = _lock(ROOT / "requirements.txt")
    assert len(lock) >= 60
    for name, (_version, hashes) in lock.items():
        assert hashes, f"{name} carries no hash"
        for item in hashes:
            assert HASH.fullmatch(item), f"{name}: {item[:40]}"
    text = (ROOT / "requirements.txt").read_text()
    for line in text.splitlines():
        stripped = line.strip()
        assert not stripped.startswith(("-e", "git+", "http://", "https://")), stripped
        assert "@" not in stripped.split("#", 1)[0].split("--hash")[0], stripped


def test_every_input_pin_is_in_the_lock_at_the_same_version_and_there_are_fifteen():
    inputs = _inputs(ROOT / "requirements.in")
    assert len(inputs) == 15
    lock = _lock(ROOT / "requirements.txt")
    for name, version in inputs.items():
        assert name in lock, name
        assert lock[name][0] == version, (name, version, lock[name][0])


def test_the_input_file_names_the_regeneration_command():
    assert REGENERATE in (ROOT / "requirements.in").read_text()


def test_the_dockerfile_pins_the_base_by_digest_and_installs_with_hash_checking():
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    assert re.fullmatch(r"FROM python:3\.14-slim@sha256:[0-9a-f]{64}", lines[0]), lines[0]
    installs = [line for line in lines if "pip install" in line]
    assert installs == [INSTALL_LINE]
