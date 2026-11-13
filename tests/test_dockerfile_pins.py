"""Every image the repository builds is pinned by digest, the proxy installs a hashed lock, and a
failed TMF921 dependency resolve stops the build.

Refresh a digest by hand with the registry command in
.planning/phases/08-performance-and-hygiene/08-RESEARCH.md (no pull, no plugin); a moved tag then
changes nothing until a person edits the Dockerfile.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILES = ["Dockerfile", "lodlive-proxy/Dockerfile", "docker/tmf921/Dockerfile"]
_DIGESTED = re.compile(r"^[^\s@:]+(?:/[^\s@:]+)*:[^\s@]+@sha256:[0-9a-f]{64}$")


def _instructions(text):
    """(line number, keyword, text) per instruction, backslash continuations joined."""
    out, pending, start = [], "", 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not pending:
            start = number
        pending = f"{pending} {line}".strip() if pending else line
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        out.append((start, pending.split(None, 1)[0].upper(), pending))
        pending = ""
    return out


def undigested_froms(text):
    """(line number, image) for each FROM that is not image:tag@sha256:<64 hex>.

    A FROM naming an earlier stage or scratch is exempt; --platform= and AS name are allowed."""
    stages, bad = set(), []
    for number, keyword, line in _instructions(text):
        if keyword != "FROM":
            continue
        words = [w for w in line.split()[1:] if not w.startswith("--")]
        image = words[0]
        if len(words) >= 3 and words[1].upper() == "AS":
            alias = words[2].lower()
        else:
            alias = None
        if image.lower() != "scratch" and image.lower() not in stages and not _DIGESTED.match(image):
            bad.append((number, image))
        if alias:
            stages.add(alias)
    return bad


@pytest.mark.parametrize("name", DOCKERFILES)
def test_every_from_names_a_tag_and_a_digest(name):
    assert undigested_froms((ROOT / name).read_text()) == []


def test_the_check_names_the_line_of_a_from_without_a_digest():
    sample = (
        "# header\n"
        "FROM python:3.14-slim@sha256:" + "a" * 64 + " AS build\n"
        "RUN true\n"
        "FROM --platform=linux/amd64 eclipse-temurin:21-jre-jammy\n"
        "FROM build\n"
        "FROM scratch\n"
        "FROM python:3.13-slim@sha256:" + "b" * 63 + "\n"
        "FROM python@sha256:" + "c" * 64 + "\n"
    )
    assert undigested_froms(sample) == [
        (4, "eclipse-temurin:21-jre-jammy"),
        (7, "python:3.13-slim@sha256:" + "b" * 63),
        (8, "python@sha256:" + "c" * 64),
    ]


def test_the_check_accepts_a_platform_flag_and_an_alias_with_a_digest():
    sample = "FROM --platform=linux/arm64 maven:3.9-eclipse-temurin-21-jammy@sha256:" + "d" * 64 + " AS b\n"
    assert undigested_froms(sample) == []


def test_the_tmf921_resolve_stops_the_build_when_it_fails():
    text = (ROOT / "docker/tmf921/Dockerfile").read_text()
    runs = [l for _, kw, l in _instructions(text) if kw == "RUN"]
    assert "RUN mvn dependency:resolve -q" in runs
    assert "RUN mvn package -DskipTests -q" in runs
    for line in runs:
        if "mvn" in line:
            assert "|" not in line and "||" not in line and ";" not in line, line


def test_the_proxy_installs_its_hashed_lock_only():
    text = (ROOT / "lodlive-proxy/Dockerfile").read_text()
    instructions = _instructions(text)
    installs = [(i, l) for i, (_, kw, l) in enumerate(instructions) if "pip install" in l]
    assert len(installs) == 1
    index, line = installs[0]
    assert line == ("RUN pip install --no-cache-dir --require-hashes --only-binary=:all: "
                    "-r requirements.txt")
    copies = [l for _, kw, l in instructions[:index] if kw == "COPY"]
    assert copies == ["COPY requirements.txt ."]
    after = [l for _, kw, l in instructions[index + 1:] if kw == "COPY"]
    assert after == ["COPY jsonp_proxy.py ."]


def _pins(path):
    """name -> version for each requirement of a lock, continuation lines joined."""
    joined = (ROOT / path).read_text().replace("\\\n", " ")
    pins = {}
    for raw in joined.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)", line)
        assert match, line
        pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    return pins


def test_the_proxy_lock_is_hashed_and_agrees_with_the_root_lock():
    text = (ROOT / "lodlive-proxy/requirements.txt").read_text()
    joined = text.replace("\\\n", " ")
    requirement_lines = [l.strip() for l in joined.splitlines()
                         if l.strip() and not l.strip().startswith(("#", "-"))]
    assert requirement_lines
    for line in requirement_lines:
        assert "--hash=sha256:" in line, line
        assert not line.startswith(("git+", "http://", "https://")), line
    proxy, root = _pins("lodlive-proxy/requirements.txt"), _pins("requirements.txt")
    assert "requests" in proxy
    assert proxy["requests"] == "2.34.2"
    assert set(proxy) <= set(root)
    assert len(proxy) <= 10
    for name, version in proxy.items():
        assert root[name] == version, name


def test_the_proxy_input_is_constrained_to_the_root_lock():
    lines = [l.strip() for l in (ROOT / "lodlive-proxy/requirements.in").read_text().splitlines()
             if l.strip() and not l.strip().startswith("#")]
    assert lines == ["-c ../requirements.txt", "requests==2.34.2"]


# ---- the pulled images of the compose file (bead contextbroker-lln) ----

def pulled_compose_images(text):
    """(service, image) for each compose service that names an image and has no build: it is pulled."""
    import yaml
    services = yaml.safe_load(text)["services"]
    return [(name, svc["image"]) for name, svc in services.items()
            if "image" in svc and "build" not in svc]


def test_every_pulled_compose_image_names_a_tag_and_a_digest():
    pulled = pulled_compose_images((ROOT / "docker-compose.yml").read_text())
    assert {name for name, _ in pulled} >= {"victoriametrics", "fuseki", "qdrant"}
    assert [(n, i) for n, i in pulled if not _DIGESTED.match(i)] == []


def test_the_compose_check_names_a_service_pinned_by_tag_only():
    text = (
        "services:\n"
        "  a:\n    image: x/y:1.0@sha256:" + "a" * 64 + "\n"
        "  b:\n    image: x/z:2.0\n"
        "  c:\n    image: local/built:local\n    build: .\n")
    pulled = pulled_compose_images(text)
    assert [(n, i) for n, i in pulled if not _DIGESTED.match(i)] == [("b", "x/z:2.0")]
