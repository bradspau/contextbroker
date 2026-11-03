"""The API image runs as a non-root user and its build context carries no
planning, tracker, operator-script or key material (ACC-05)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _lines(name):
    out = []
    for raw in (ROOT / name).read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _instructions():
    return [(l.split(None, 1)[0].upper(), l) for l in _lines("Dockerfile")]


def test_dockerfile_has_exactly_one_user_instruction():
    users = [l for kw, l in _instructions() if kw == "USER"]
    assert len(users) == 1, users


def test_user_is_not_root():
    (user_line,) = [l for kw, l in _instructions() if kw == "USER"]
    value = user_line.split(None, 1)[1].strip()
    assert value not in ("root", "0", "0:0", "root:root")
    assert value.split(":")[0] not in ("root", "0")


def test_user_comes_after_the_last_copy_and_before_cmd():
    kws = [kw for kw, _ in _instructions()]
    user_at = kws.index("USER")
    last_copy = max(i for i, kw in enumerate(kws) if kw == "COPY")
    assert last_copy < user_at < kws.index("CMD")


def test_cmd_is_unchanged():
    cmd = [l for kw, l in _instructions() if kw == "CMD"]
    assert cmd == ['CMD ["uvicorn", "broker_engine.main:app", "--host", "0.0.0.0", "--port", "8080"]']


def test_dockerignore_excludes_planning_tracker_operator_and_key_material():
    lines = _lines(".dockerignore")
    for entry in (".beads/", ".planning/", ".claude/", "design/", "scripts/", "CLAUDE.md",
                  "AGENTS.md", "planning.md", "*.pem", "*.key", ".env"):
        assert entry in lines, entry


def test_dockerignore_keeps_the_licensed_patterns():
    lines = _lines(".dockerignore")
    for entry in ("ontologies/licensed", "**/ontology/**/*.ttl", "ontologies/tbox/*.ttl",
                  "!ontologies/tbox/topology.ttl", "ontologies/**/*.zip"):
        assert entry in lines, entry
